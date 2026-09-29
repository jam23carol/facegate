# -*- coding: utf-8 -*-
"""Админ-панель (Flask): страницы, HTTP API, MJPEG-поток, авторизация.

Что умеет:
  * **Камеры** — живое видео со всех подключённых клиентов (MJPEG) с рамками
    распознанных лиц поверх; снимки кадров на сервер; объявления клиентам;
    **добавление нового лица прямо из текущего кадра**.
  * **Люди** — добавление/удаление/переименование эталонов, отдельные фото,
    статус и пересинтез озвучки, ручная проверка приветствия.
  * **Саундборд** — библиотека записанных звуков: загрузка, воспроизведение,
    трансляция клиентам, скрытие и удаление.
  * **Настройки** — порог схожести, кулдаун, модель детекции, пул фраз
    приветствия, движок/голос/скорость TTS, качество и FPS трансляции —
    применяются на лету.
  * **Журнал** — лог сервера и события распознавания в реальном времени.

Доступ защищён логином/паролем (:mod:`facegate.web.auth`) и CSRF-токеном сессии.

Что исправлено в этой версии
----------------------------
* **«Добавить лицо из кадра» больше не вешает сервер.** Все вызовы dlib
  проходят через глобальную блокировку инференса
  (:mod:`facegate.recognition.engine`), а эмбеддинг для нового эталона
  вычисляется **по найденной рамке исходного кадра** — повторная детекция на
  плотно обрезанном кропе (которая почти всегда возвращала «лицо не найдено»
  и 422) не выполняется. Если движок всё же занят дольше отведённого времени,
  панель получает 503 с понятным текстом вместо зависания всего процесса.
* **Объявления**: кэш озвучки проверяется синхронно, готовая фраза уходит
  **одной** командой со звуком. Пустая команда ``speak`` без ``audio_b64``
  больше не отправляется вовсе; если фраза новая — панель коротко ждёт синтез
  (настройка ``announce_wait``), а недосинтезированную озвучку досылает фоновый
  поток (с уже захваченным именем пользователя — раньше обращение к
  ``session`` из чужого потока роняло этот поток, и звук не приходил).
* **«Проверить привет»** так же сначала дожидается аудио и отправляет команду
  с реальной (случайной) фразой из пула.
* **Адресаты команд** считаются по мягкому таймауту
  (:meth:`facegate.frames.FrameHub.command_targets`) — короткая потеря
  видеокадров больше не даёт 409 «Нет клиентов в эфире».
* **Файлы больше не «теряются» (500/404).** Звуки саундборда, фото эталонов и
  снимки отдаются по **абсолютным** путям: Flask разрешает относительные пути
  от каталога модуля приложения (``facegate/web/``), а не от cwd процесса,
  из-за чего ``send_file``/``send_from_directory`` не находили существующие
  файлы. Если файла реально нет на диске — честный 404 вместо 500.
* **Понятная статистика.** В шапке панели — текущие значения: клиентов онлайн,
  лиц в кадре **прямо сейчас** (``FrameHub.totals()["current_faces"]``, только
  онлайн-клиенты) и число приветствий; приветствия дополнительно ведутся
  **по каждому человеку** (:meth:`facegate.stats.Stats.incr_greet`,
  ``greet_count`` в ``/api/persons``, ``greets_by_person`` в ``/api/status``).
"""
import hmac
import logging
import os
import secrets
import threading
import time
from functools import wraps
from urllib.parse import quote

from flask import (Flask, Response, abort, jsonify, redirect, request,
                   send_file, send_from_directory, session, url_for)

from facegate import VERSION
from facegate.commands import build_greet_command, build_speak_command
from facegate.config import VOICE_AFFECTING, Settings
from facegate.recognition.engine import InferenceBusyError
from facegate.events import EventBus, LogRingHandler
from facegate.frames import FrameHub, mjpeg_generator, translit
from facegate.recognition import engine as recog
from facegate.recognition.store import IMAGE_EXTS, sanitize_name
from facegate.soundboard import Soundboard
from facegate.stats import Stats, human_uptime
from facegate.tts.cache import format_template
from facegate.web import ui as admin_ui
from facegate.web.auth import AdminAuth

log = logging.getLogger(__name__)

# Отступы кропа при сохранении лица из кадра (в долях размера рамки).
# Больше контекста = выше шанс, что детектор найдёт лицо при перезагрузке базы.
CAPTURE_CROP_MARGIN = 0.5

# Сколько секунд кнопка «проверить привет» ждёт синтез, чтобы команда ушла
# сразу со звуком (иначе озвучка досылается фоновым потоком).
GREET_WAIT_SECONDS = float(os.environ.get("FACEGATE_GREET_WAIT", "8") or 8)

NOT_FOUND_PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>404 — не найдено</title>
<style>body{background:#12141a;color:#e8eaf0;font-family:'Segoe UI',system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{text-align:center}.box h1{font-size:64px;margin:0;color:#4f8cff}
a{color:#4f8cff;text-decoration:none;border:1px solid #2a3040;padding:8px 14px;
border-radius:8px;display:inline-block;margin-top:14px}</style></head>
<body><div class="box"><h1>404</h1><p>Страница или ресурс не найдены.</p>
<a href="/">← в админ-панель</a></div></body></html>"""


def template_pool(settings):
    """Пул фраз приветствия из настроек (с совместимостью со старым полем)."""
    pool = list(getattr(settings, "greet_template_list", None) or [])
    if not pool:
        single = str(getattr(settings, "greet_template", "") or "").strip()
        pool = [single] if single else []
    return pool


def command_targets(hub, client_id=None):
    """Адресаты команд панели: мягкий таймаут «в эфире» + явный клиент."""
    getter = getattr(hub, "command_targets", None)
    if callable(getter):
        try:
            return list(getter(client_id))
        except TypeError:  # pragma: no cover - старая подпись хаба
            pass
    if client_id:
        return [client_id]
    return list(hub.online_clients())


def create_app(store, voice, faces_dir, hub=None, settings=None, stats=None,
               events=None, log_handler=None, auth=None, throttle=None,
               sender=None, debug_dir="debug_frames", protect=None,
               soundboard=None, version=VERSION):
    """Собирает Flask-приложение.

    Все аргументы кроме ``store``, ``voice``, ``faces_dir`` опциональны — при
    отсутствии создаются заглушки, поэтому панель можно поднять и в тестах.
    ``protect`` по умолчанию равен ``auth.enabled``.
    """
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
    app.config["CSRF_ENABLED"] = True

    hub = hub or FrameHub()
    settings = settings or Settings()
    stats = stats or Stats()
    events = events if events is not None else EventBus()
    log_handler = log_handler or LogRingHandler()
    auth = auth or AdminAuth(enabled=False)
    soundboard = soundboard or Soundboard()
    protect = auth.enabled if protect is None else bool(protect)

    app.secret_key = auth.secret_key if protect else (
        os.environ.get("ADMIN_SECRET_KEY") or secrets.token_hex(32))
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = 12 * 3600
    # человекочитаемые имена в JSON (кириллица без \uXXXX)
    app.config["JSON_AS_ASCII"] = False
    try:
        app.json.ensure_ascii = False
    except AttributeError:  # Flask < 2.2
        pass

    # ---------------- авторизация ----------------
    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def csrf_ok():
        if not protect or not app.config.get("CSRF_ENABLED", True):
            return True
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        expected = session.get("csrf")
        if not token or not expected:
            return False
        return hmac.compare_digest(str(token), str(expected))

    def current_user():
        return session.get("admin_user") if protect else "admin"

    def is_api():
        return request.path.startswith("/api/") or request.path.startswith("/stream/")

    def login_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not protect or current_user():
                return fn(*args, **kwargs)
            if is_api():
                return jsonify({"ok": False, "error": "Требуется вход"}), 401
            return redirect(url_for("login", next=request.path))
        return wrapper

    def write_protected(fn):
        """Проверка CSRF для изменяющих запросов."""
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if request.method in ("POST", "PUT", "DELETE", "PATCH") and not csrf_ok():
                return jsonify({"ok": False, "error": "Недействительный CSRF-токен, обновите страницу"}), 403
            return fn(*args, **kwargs)
        return wrapper

    # ---------------- служебное ----------------
    def _sync_dependents(settings, hub, throttle, voice, store, changed=None):
        """Применяет настройки к зависимым объектам (hub, throttle, voice)."""
        hub.configure(jpeg_quality=settings.jpeg_quality,
                      stream_fps=settings.stream_fps,
                      stale_after=settings.stale_after,
                      command_stale_after=settings.get("command_stale_after"),
                      stream_limit=settings.get("stream_limit"))
        if throttle is not None:
            throttle.cooldown = float(settings.greet_cooldown)
        if voice is not None:
            voice.configure(template=settings.greet_template,
                            templates=template_pool(settings),
                            rate=settings.tts_rate,
                            voice_hint=settings.tts_voice,
                            engine=settings.tts_engine,
                            speed=settings.tts_speed)
        if changed and set(changed) & set(VOICE_AFFECTING):
            names = store.names() if store else []
            if names and voice is not None:
                voice.rebuild(names)
                log.info("Озвучка пересинтезируется для %d чел. (изменились %s)",
                         len(names), ", ".join(sorted(set(changed))))
                return len(names)
        return 0

    # первичная синхронизация зависимых объектов с настройками
    _sync_dependents(settings, hub, throttle, voice, store)

    def _warn_default_password():
        if not protect:
            return ""
        if auth.is_default_credentials():
            return ('<div class="warnbar" id="warnbar">⚠️ Используется пароль по умолчанию '
                    '(<code>admin</code> / <code>admin</code>). Смените его: кнопка '
                    '«пароль» в правом верхнем углу.</div>')
        return ""

    def _boot_payload():
        return admin_ui.boot_json(
            version=version,
            user=current_user(),
            settings=settings.to_dict(),
            must_change_password=protect and auth.is_default_credentials(),
            auth_enabled=protect,
            persons=len(store.names()) if store else 0,
        )

    # ---------------- страницы ----------------
    @app.get("/")
    @login_required
    def index():
        html = admin_ui.render(
            admin_ui.ADMIN_PAGE,
            CSS=admin_ui.CSS, JS=admin_ui.JS, FAVICON=admin_ui.FAVICON,
            CSRF=csrf_token(), USERNAME=current_user() or "admin",
            VERSION=version, WARNBAR=_warn_default_password(), BOOT=_boot_payload(),
        )
        return Response(html, mimetype="text/html; charset=utf-8")

    @app.get("/camera/<path:client_id>")
    @login_required
    def camera_page(client_id):
        html = admin_ui.render(
            admin_ui.CAMERA_PAGE,
            CSS=admin_ui.CSS, FAVICON=admin_ui.FAVICON,
            CLIENT=client_id,
            CLIENT_ENC=quote(client_id, safe=""),
        )
        return Response(html, mimetype="text/html; charset=utf-8")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not protect:
            return redirect(url_for("index"))
        error = ""
        if request.method == "POST":
            if not csrf_ok():
                error = "Сессия устарела, попробуйте ещё раз"
            else:
                username = (request.form.get("username") or "").strip()
                password = request.form.get("password") or ""
                client_key = f"{request.remote_addr}|{username}"
                if auth.is_locked(client_key):
                    error = "Слишком много попыток, подождите 5 минут"
                elif auth.verify(username, password, client_key=client_key):
                    session.clear()
                    session["admin_user"] = username
                    session.permanent = True
                    auth.reset_failures(client_key)
                    log.info("Вход в админ-панель: %s (%s)", username, request.remote_addr)
                    events.push("login", user=username, ip=request.remote_addr)
                    nxt = request.form.get("next") or "/"
                    if not nxt.startswith("/") or nxt.startswith("//"):
                        nxt = "/"
                    return redirect(nxt)
                else:
                    error = "Неверный пользователь или пароль"
                    log.warning("Неудачный вход: %s (%s)", username, request.remote_addr)
        html = admin_ui.render(
            admin_ui.LOGIN_PAGE,
            CSS=admin_ui.CSS, FAVICON=admin_ui.FAVICON, VERSION=version,
            CSRF=csrf_token(), ERROR=error,
            NEXT=request.args.get("next", "/") if request.method == "GET" else "/",
        )
        return Response(html, mimetype="text/html; charset=utf-8"), (200 if not error else 401)

    @app.post("/logout")
    def logout():
        if not csrf_ok():
            return jsonify({"ok": False, "error": "CSRF"}), 403
        user = session.pop("admin_user", None)
        if user:
            log.info("Выход из админ-панели: %s", user)
        return redirect(url_for("login"))

    # ---------------- видео ----------------
    @app.get("/stream/<path:client_id>")
    @login_required
    def stream(client_id):
        boundary = "faceframe"
        # Генератор сам освобождает слот потока при закрытии вкладки
        # (GeneratorExit) — см. facegate.frames.mjpeg_generator
        return Response(
            mjpeg_generator(hub, client_id, boundary=boundary.encode("ascii")),
            mimetype=f"multipart/x-mixed-replace; boundary={boundary}",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                     "Pragma": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "close"})

    @app.get("/api/cameras")
    @login_required
    def api_cameras():
        return jsonify({"cameras": hub.clients_info(), "totals": hub.totals()})

    @app.get("/api/cameras/<path:client_id>")
    @login_required
    def api_camera(client_id):
        info = hub.client_info(client_id)
        if info is None:
            return jsonify({"ok": False, "error": "Клиент не найден"}), 404
        return jsonify(info)

    @app.delete("/api/cameras/<path:client_id>")
    @login_required
    @write_protected
    def api_forget_camera(client_id):
        removed = hub.forget(client_id)
        if removed:
            events.push("client_gone", client_id=client_id, text="убран вручную из панели")
        return jsonify({"ok": removed})

    @app.get("/api/cameras/<path:client_id>/frame.jpg")
    @login_required
    def api_camera_frame(client_id):
        data = hub.latest_annotated_jpeg(client_id)
        if data is None:
            raw, _seq = hub.latest_raw(client_id)
            data = raw
        if data is None:
            return jsonify({"ok": False, "error": "Нет кадров от клиента"}), 404
        safe_name = translit(client_id).replace(" ", "_")[:40] or "frame"
        # ?download=1 — браузер сохраняет файл, а не открывает JPEG в текущей
        # вкладке (раньше переход по ссылке «скачать кадр» уводил из панели)
        want_download = request.args.get("download") in ("1", "true", "yes")
        disposition = "attachment" if want_download else "inline"
        return Response(data, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store",
                                 "Content-Disposition":
                                     f'{disposition}; filename="{safe_name}.jpg"'})

    @app.post("/api/cameras/<path:client_id>/snapshot")
    @login_required
    @write_protected
    def api_camera_snapshot(client_id):
        path = hub.save_snapshot(client_id, directory=debug_dir, annotated=True)
        if not path:
            return jsonify({"ok": False, "error": "Нет кадра для сохранения"}), 404
        stats.incr("snapshots_saved")
        return jsonify({"ok": True, "path": path,
                        "name": os.path.basename(path),
                        "url": url_for("saved_snapshot", fname=os.path.basename(path))})

    @app.get("/api/snapshots")
    @login_required
    def api_snapshots():
        return jsonify({"dir": debug_dir, "snapshots": hub.list_snapshots(debug_dir)})

    @app.get("/snapshots/<path:fname>")
    @login_required
    def saved_snapshot(fname):
        if not fname.lower().endswith(IMAGE_EXTS) or os.path.basename(fname) != fname:
            abort(404)
        # абсолютный путь: send_from_directory иначе ищет от facegate/web/
        abs_dir = os.path.abspath(debug_dir)
        if not os.path.exists(os.path.join(abs_dir, fname)):
            abort(404)
        return send_from_directory(abs_dir, fname)

    # ---------------- лица в текущем кадре ----------------
    def _latest_frame_or_error(client_id):
        frame = hub.latest_raw_frame(client_id)
        if frame is None:
            return None, (jsonify({"ok": False, "error": "Нет кадров от клиента"}), 404)
        return frame, None

    def _detect_or_error(frame):
        """Детекция лиц для панели. Никогда не блокируется навсегда.

        ``InferenceBusyError`` — движок dlib занят потоком распознавания дольше
        отведённого таймаута: возвращаем 503 и понятный текст вместо зависания
        воркера (раньше здесь возникал дедлок OpenMP, вешавший весь процесс).
        """
        try:
            return recog.detect_faces_for_capture(frame, model=settings.detect_model), None
        except InferenceBusyError as e:
            log.warning("Детекция для панели отклонена: %s", e)
            return None, (jsonify({"ok": False, "error": str(e), "busy": True}), 503)
        except RuntimeError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 503)

    @app.get("/api/cameras/<path:client_id>/faces")
    @login_required
    def api_camera_faces(client_id):
        """Список лиц в последнем кадре клиента — для добавления нового человека."""
        frame, err = _latest_frame_or_error(client_id)
        if err:
            return err
        faces, err = _detect_or_error(frame)
        if err:
            return err
        entries = store.entries()
        items = []
        h, w = frame.shape[:2]
        for i, (box, enc) in enumerate(faces):
            name, distance = recog.recognize_face(entries, enc, settings.threshold)
            items.append({
                "index": i, **box,
                "known": bool(name), "name": name,
                "distance": round(distance, 4),
                "preview_url": url_for("api_camera_face_crop", client_id=client_id,
                                       left=box["left"], top=box["top"],
                                       right=box["right"], bottom=box["bottom"],
                                       margin=0.35),
            })
        return jsonify({"ok": True, "client_id": client_id,
                        "width": int(w), "height": int(h), "faces": items})

    @app.get("/api/cameras/<path:client_id>/face.jpg")
    @login_required
    def api_camera_face_crop(client_id):
        """Кроп лица из последнего кадра (превью в диалоге добавления)."""
        frame, err = _latest_frame_or_error(client_id)
        if err:
            return err
        try:
            left = request.args.get("left", type=int)
            top = request.args.get("top", type=int)
            right = request.args.get("right", type=int)
            bottom = request.args.get("bottom", type=int)
            margin = float(request.args.get("margin", 0.35))
        except (TypeError, ValueError):
            left = top = right = bottom = None
            margin = 0.35
        if None in (left, top, right, bottom) or right <= left or bottom <= top:
            return jsonify({"ok": False, "error": "Некорректные координаты лица"}), 400
        crop = recog.crop_face(frame, left, top, right, bottom,
                               margin=max(0.0, min(1.0, margin)))
        data = recog.encode_jpeg(crop, quality=90) if crop is not None else None
        if data is None:
            return jsonify({"ok": False, "error": "Не удалось вырезать лицо"}), 400
        return Response(data, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    # ---------------- статус / журнал ----------------
    @app.get("/api/status")
    @login_required
    def api_status():
        counters = stats.snapshot()
        totals = hub.totals()
        payload = {
            "ok": True,
            "version": version,
            "user": current_user(),
            "uptime_sec": counters["uptime_sec"],
            "uptime_human": human_uptime(counters["uptime_sec"]),
            "counters": counters,
            "clients_online": totals["clients_online"],
            "clients_total": totals["clients_total"],
            # лица в кадре ПРЯМО СЕЙЧАС (по онлайн-клиентам), а не накопительно
            "current_faces": totals.get("current_faces", totals.get("faces", 0)),
            "active_streams": totals.get("active_streams", 0),
            "stream_limit": totals.get("stream_limit", 0),
            "totals": totals,
            "settings": settings.to_dict(),
            "throttle": throttle.snapshot() if throttle is not None else {},
            "commands": sender.stats() if sender is not None else {},
            "voice": _voice_summary(),
            "persons": len(store.names()),
            "auth_enabled": protect,
            "must_change_password": protect and auth.is_default_credentials(),
            "server_time": time.time(),
        }
        return jsonify(payload)

    @app.get("/api/logs")
    @login_required
    def api_logs():
        since = request.args.get("since", 0, type=int)
        limit = min(request.args.get("limit", 300, type=int), 1000)
        lines, next_seq = log_handler.since(since, limit)
        return jsonify({"lines": lines, "next": next_seq})

    @app.get("/api/events")
    @login_required
    def api_events():
        since = request.args.get("since", 0, type=int)
        limit = min(request.args.get("limit", 200, type=int), 500)
        items, next_seq = events.since(since, limit)
        return jsonify({"events": items, "next": next_seq})

    @app.get("/api/health")
    def api_health():
        """Без авторизации — для healthcheck контейнера."""
        totals = hub.totals()
        return jsonify({"status": "ok", "version": version,
                        "uptime_sec": round(time.time() - stats.started_at, 1),
                        "clients_online": totals["clients_online"],
                        "persons": len(store.names())})

    # ---------------- настройки ----------------
    @app.get("/api/settings")
    @login_required
    def api_settings_get():
        return jsonify(settings.describe())

    @app.post("/api/settings")
    @login_required
    @write_protected
    def api_settings_set():
        patch = (request.get_json(silent=True) or {}).get("settings")
        if patch is None:
            patch = request.form.to_dict()
        if not isinstance(patch, dict) or not patch:
            return jsonify({"ok": False, "error": "Пустой список изменений"}), 400
        applied, errors, voice_changed = settings.update(patch)
        rebuilt = _sync_dependents(settings, hub, throttle, voice, store, changed=applied.keys())
        for key, value in applied.items():
            events.push("settings", text=f"{key} = {value}", user=current_user())
            log.info("Настройка изменена: %s = %r (пользователь %s)", key, value, current_user())
        return jsonify({"ok": not errors, "applied": applied, "errors": errors,
                        "voice_rebuild": rebuilt, "settings": settings.to_dict()}), \
            (200 if applied or not errors else 422)

    @app.post("/api/throttle/reset")
    @login_required
    @write_protected
    def api_throttle_reset():
        name = (request.get_json(silent=True) or {}).get("name") if request.is_json else None
        if throttle is None:
            return jsonify({"ok": False, "error": "Троттлинг не инициализирован"}), 400
        throttle.reset(name)
        events.push("throttle_reset", text=name or "все", user=current_user())
        return jsonify({"ok": True, "reset": name or "all"})

    # ---------------- озвучка ----------------
    def _random_phrase(name):
        """(текст, WAV) случайной фразы приветствия без повтора предыдущей."""
        picker = getattr(voice, "get_random", None)
        if callable(picker):
            try:
                text, wav = picker(name)
                return (text or format_template(getattr(voice, "template", ""), name)), wav
            except Exception as e:  # pragma: no cover - заглушки в тестах
                log.warning("Не удалось выбрать вариант озвучки для %s: %s", name, e)
        return format_template(getattr(voice, "template", ""), name), voice.get(name)

    def _wait_phrase(name, timeout):
        """Ждёт готовности озвучки человека: (text, wav|None)."""
        deadline = time.time() + max(0.0, float(timeout))
        text, wav = _random_phrase(name)
        while wav is None and time.time() < deadline:
            if voice.status(name) == "error":
                break
            time.sleep(0.1)
            text, wav = _random_phrase(name)
        return text, wav

    def _voice_summary():
        names = store.names()
        ready = pending = errors = 0
        error_list = []
        for name in names:
            st = voice.status(name)
            if st == "ready":
                ready += 1
            elif st == "pending":
                pending += 1
            elif st == "error":
                errors += 1
                error_list.append(name)
        try:
            info = voice.cache_info()
        except Exception:  # pragma: no cover - заглушки в тестах
            info = {}
        return {"ready": ready, "pending": pending, "errors": errors,
                "error_list": error_list, "total": len(names),
                "files": info.get("files", 0), "bytes": info.get("bytes", 0),
                "dir": info.get("dir", getattr(voice, "cache_dir", "")),
                "engine": info.get("engine", "?"),
                "engine_pref": info.get("engine_pref", "auto"),
                "available_engines": info.get("available_engines", {})}

    @app.get("/api/voice-info")
    @login_required
    def api_voice_info():
        summary = _voice_summary()
        qsize = 0
        queue_obj = getattr(voice, "_queue", None)
        if queue_obj is not None and hasattr(queue_obj, "qsize"):
            qsize = queue_obj.qsize()
        summary.update({
            "pending_queue": qsize,
            "template": voice.template,
            "templates": list(getattr(voice, "templates", None) or [voice.template]),
            "variants": len(getattr(voice, "templates", None) or [voice.template]),
        })
        return jsonify(summary)

    @app.post("/api/voice-info/rebuild")
    @login_required
    @write_protected
    def api_voice_rebuild():
        names = store.names()
        if hasattr(voice, "rebuild"):
            voice.rebuild(names)
        else:  # pragma: no cover - заглушка в тестах
            for n in names:
                voice.ensure(n, force=True)
        events.push("voice_rebuild", text=f"{len(names)} чел.", user=current_user())
        return jsonify({"ok": True, "queued": len(names)})

    def _cached_announce_wav(text):
        """Готовая озвучка фразы из кэша — без запуска синтеза (или None)."""
        getter = getattr(voice, "cached_text_wav", None)
        if not callable(getter):
            return None
        try:
            return getter(text)
        except Exception as e:  # pragma: no cover - кэш не должен ломать объявление
            log.warning("Не удалось прочитать кэш озвучки объявления: %s", e)
            return None

    def _wait_announce_wav(text, timeout):
        """Ждёт синтез фразы не дольше ``timeout`` сек (single-flight, без дублей)."""
        if not timeout or timeout <= 0:
            return None
        waiter = getattr(voice, "wait_text", None)
        if callable(waiter):
            try:
                return waiter(text, timeout=timeout)
            except Exception as e:  # pragma: no cover
                log.error("Синтез объявления не завершился: %s", e)
                return None
        synth = getattr(voice, "synthesize_now", None)
        if callable(synth):  # pragma: no cover - совместимость со старым кэшем
            try:
                return synth(text, timeout=timeout)
            except Exception as e:
                log.error("Синтез объявления не завершился: %s", e)
        return None

    def _tts_available():
        checker = getattr(voice, "engine_available", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:  # pragma: no cover
                return True
        return True

    @app.post("/api/announce")
    @login_required
    @write_protected
    def api_announce():
        """Объявление клиентам — одной командой, всегда со звуком.

        Раньше эндпоинт безусловно отправлял пакет ``speak`` с ``audio_b64=null``
        (клиент писал «Команда без аудио» и молчал), а озвучку досылал вторым
        пакетом из фонового потока — даже если WAV уже лежал в кэше. Теперь:

        1. кэш проверяется **синхронно**: готовая фраза уходит сразу со звуком;
        2. если фразы в кэше нет — ждём синтез не дольше ``announce_wait`` сек
           и тоже отправляем один пакет со звуком;
        3. если синтез долгий — команда уходит из фонового потока, когда WAV
           будет готов. Пустой пакет не отправляется никогда.
        """
        data = request.get_json(silent=True) or request.form.to_dict()
        text = str(data.get("text") or "").strip()[:400]
        if not text:
            return jsonify({"ok": False, "error": "Пустой текст"}), 400
        if sender is None:
            return jsonify({"ok": False, "error": "Отправка команд недоступна (нет PUB-сокета)"}), 503
        client_id = data.get("client_id")
        targets = command_targets(hub, client_id)
        if not targets:
            return jsonify({"ok": False, "error": "Нет клиентов в эфире"}), 409
        # имя пользователя захватываем ЗДЕСЬ: в фоновом потоке контекста запроса
        # (и session) уже нет — раньше это роняло поток и звук не приходил
        user = current_user() or "admin"

        wav = _cached_announce_wav(text)
        waited = False
        if wav is None:
            wait_sec = float(settings.get("announce_wait") or 0.0)
            wav = _wait_announce_wav(text, min(wait_sec, 60.0))
            waited = wav is not None

        if wav:
            payload = build_speak_command(text, wav, source=user)
            sent = sender.broadcast(payload, targets)
            stats.incr("manual_announces", sent)
            events.push("announce", text=text, client_id=client_id or ", ".join(targets),
                        user=user, audio=True, cached=not waited)
            log.info("📢 Объявление отправлено %d клиентам со звуком (%s): %s",
                     sent, "кэш" if not waited else "синтез", text[:60])
            return jsonify({"ok": True, "sent": sent, "targets": targets,
                            "voice": "ready", "cached": not waited})

        if not _tts_available():
            # озвучки в этом окружении нет вовсе — отправляем текст, чтобы
            # событие хотя бы дошло до клиента (в логе клиента будет видно)
            payload = build_speak_command(text, None, source=user)
            sent = sender.broadcast(payload, targets)
            stats.incr("manual_announces", sent)
            events.push("announce", text=text, client_id=client_id or ", ".join(targets),
                        user=user, audio=False)
            return jsonify({"ok": True, "sent": sent, "targets": targets,
                            "voice": "unavailable",
                            "warning": "TTS-движок недоступен — отправлен только текст"}), 200

        events.push("announce", text=text, client_id=client_id or ", ".join(targets),
                    user=user, audio="pending")
        threading.Thread(target=_announce_with_voice, args=(text, targets, user),
                         daemon=True, name="announce-tts").start()
        log.info("📢 Объявление «%s» поставлено на синтез, звук уйдёт %d клиентам",
                 text[:60], len(targets))
        return jsonify({"ok": True, "sent": 0, "targets": targets, "voice": "pending"})

    def _announce_with_voice(text, targets, user):
        """Досылает объявление со звуком, когда синтез завершится (без пустых пакетов)."""
        wav = None
        try:
            wav = _wait_announce_wav(text, timeout=max(60.0, float(
                settings.get("announce_wait") or 0.0) * 4))
            if wav is None:
                synth = getattr(voice, "synthesize_now", None)
                wav = synth(text) if callable(synth) else None
        except Exception as e:  # pragma: no cover
            log.error("Не удалось озвучить объявление: %s", e)
        if not wav:
            log.warning("Объявление «%s» не озвучено — команда клиентам не отправлена",
                        text[:60])
            events.push("announce_error", text=text,
                        error="озвучка недоступна (см. лог TTS)")
            return
        sent = sender.broadcast(build_speak_command(text, wav, source=user), targets)
        stats.incr("manual_announces", sent)
        log.info("Объявление озвучено и отправлено %d клиентам", sent)

    # ---------------- саундборд ----------------
    @app.get("/api/soundboard")
    @login_required
    def api_soundboard():
        include_hidden = request.args.get("include_hidden") in ("1", "true", "yes")
        items = soundboard.list_items(include_hidden=include_hidden)
        return jsonify({"ok": True, "items": items, "dir": soundboard.sounds_dir,
                        "ffmpeg": soundboard.ffmpeg_available()})

    @app.post("/api/soundboard/upload")
    @login_required
    @write_protected
    def api_soundboard_upload():
        f = request.files.get("file")
        if f is None:
            return jsonify({"ok": False, "error": "Файл не передан (поле file)"}), 400
        title = (request.form.get("title") or "").strip()[:80] or None
        data = f.read()
        ok, result = soundboard.add_upload(f.filename or "", data, title=title)
        if not ok:
            return jsonify({"ok": False, "error": result}), 422
        events.push("sound_uploaded", text=result.get("title"), user=current_user())
        return jsonify({"ok": True, "item": result})

    @app.post("/api/soundboard/<path:item_id>/hide")
    @login_required
    @write_protected
    def api_soundboard_hide(item_id):
        data = request.get_json(silent=True) or request.form.to_dict()
        hidden = data.get("hidden", True)
        if isinstance(hidden, str):
            hidden = hidden.strip().lower() in ("1", "true", "yes")
        if soundboard.find(item_id) is None:
            return jsonify({"ok": False, "error": "Элемент не найден"}), 404
        soundboard.set_hidden(item_id, bool(hidden))
        events.push("sound_hidden" if hidden else "sound_shown",
                    text=item_id, user=current_user())
        return jsonify({"ok": True, "id": item_id, "hidden": bool(hidden)})

    @app.delete("/api/soundboard/<path:item_id>")
    @login_required
    @write_protected
    def api_soundboard_delete(item_id):
        ok, message = soundboard.delete(item_id)
        if not ok:
            return jsonify({"ok": False, "error": message}), 404
        events.push("sound_deleted", text=item_id, user=current_user())
        return jsonify({"ok": True})

    @app.get("/api/soundboard/<path:item_id>/file")
    @login_required
    def api_soundboard_file(item_id):
        item = soundboard.resolve(item_id)
        if item is None:
            return jsonify({"ok": False, "error": "Элемент не найден"}), 404
        # send_file() трактует относительный путь ОТНОСИТЕЛЬНО КОРНЕВОГО
        # КАТАЛОГА ПРИЛОЖЕНИЯ (facegate/web/), а не от cwd процесса — поэтому
        # voice_cache/... «не находился» и падал в 500. Приводим к абсолютному.
        file_path = os.path.abspath(item.get("path") or "")
        if not os.path.isfile(file_path):
            return jsonify({"ok": False, "error": "Файл звука не найден на диске"}), 404
        return send_file(file_path, mimetype=item.get("mimetype") or "audio/wav",
                         download_name=item.get("filename"), conditional=True)

    @app.post("/api/soundboard/<path:item_id>/broadcast")
    @login_required
    @write_protected
    def api_soundboard_broadcast(item_id):
        """Транслирует звук клиентам (команда speak с WAV)."""
        if sender is None:
            return jsonify({"ok": False, "error": "Отправка команд недоступна (нет PUB-сокета)"}), 503
        item = soundboard.resolve(item_id)
        if item is None:
            return jsonify({"ok": False, "error": "Элемент не найден"}), 404
        data = request.get_json(silent=True) or {}
        client_id = data.get("client_id")
        targets = command_targets(hub, client_id)
        if not targets:
            return jsonify({"ok": False, "error": "Нет клиентов в эфире"}), 409
        wav, err = soundboard.wav_bytes_for_broadcast(item_id)
        if wav is None:
            return jsonify({"ok": False, "error": err or "Не удалось подготовить аудио"}), 422
        payload = build_speak_command(item.get("title") or item_id, wav,
                                      source=f"soundboard:{current_user() or 'admin'}")
        sent = sender.broadcast(payload, targets)
        stats.incr("soundboard_plays", sent)
        events.push("sound_broadcast", text=item.get("title") or item_id,
                    client_id=client_id or ", ".join(targets), user=current_user())
        return jsonify({"ok": True, "sent": sent, "targets": targets})

    # ---------------- люди ----------------
    def _note_greet(name):
        """Приветствие отправлено: общий счётчик + персональная статистика."""
        counter = getattr(stats, "incr_greet", None)
        if callable(counter):
            counter(name)
        else:  # pragma: no cover - заглушки stats в тестах
            stats.incr("greets_sent")

    @app.get("/api/persons")
    @login_required
    def api_persons():
        persons = []
        pool = template_pool(settings) or [getattr(voice, "template", "") or ""]
        greets_getter = getattr(stats, "greets_by_person", None)
        greets_by_person = greets_getter() if callable(greets_getter) else {}
        for name in store.names():
            info = _variants_info(name)
            persons.append({
                "name": name,
                "photos": store.photos(name),
                "voice_status": voice.status(name),
                "phrase": format_template(pool[0], name),
                "phrases": info["texts"] or [format_template(pool[0], name)],
                "voice_variants": info["total"],
                "voice_ready_variants": info["ready"],
                # сколько раз сервер поздоровался с этим человеком
                "greet_count": int(greets_by_person.get(name, 0) or 0),
            })
        return jsonify({"persons": persons, "templates": pool})

    def _variants_info(name):
        """Сколько вариантов озвучки человека готово (пул фраз приветствия)."""
        getter = getattr(voice, "variants_info", None)
        if callable(getter):
            try:
                info = getter(name) or {}
                return {"total": int(info.get("total") or 0),
                        "ready": int(info.get("ready") or 0),
                        "texts": list(info.get("texts") or [])}
            except Exception:  # pragma: no cover - заглушки в тестах
                pass
        return {"total": 1, "ready": 1 if voice.status(name) == "ready" else 0,
                "texts": [format_template(getattr(voice, "template", ""), name)]}

    @app.post("/api/persons")
    @login_required
    @write_protected
    def api_add():
        name = request.form.get("name", "")
        files = request.files.getlist("photos")
        if not sanitize_name(name):
            return jsonify({"ok": False, "error": "Некорректное имя", "results": []}), 400
        if not files:
            return jsonify({"ok": False, "error": "Файлы не переданы", "results": []}), 400
        results = []
        added_any = False
        for f in files:
            data = f.read()
            ok, message = store.add_photo(name, data)
            results.append({"ok": ok, "message": message,
                            "filename": os.path.basename(f.filename or "")})
            added_any = added_any or ok
        voice_status = None
        person_key = sanitize_name(name)
        if added_any:
            voice.ensure(person_key)
            voice_status = voice.status(person_key)
            events.push("person_added", text=f"{person_key} (+{sum(1 for r in results if r['ok'])} фото)",
                        user=current_user())
        return jsonify({"ok": added_any, "results": results, "voice": voice_status,
                        "name": person_key}), (200 if added_any else 422)

    @app.post("/api/persons/from-frame")
    @login_required
    @write_protected
    def api_add_from_frame():
        """Добавить нового человека прямо из текущего кадра камеры.

        Тело запроса (JSON): ``client_id``, ``name`` и координаты лица
        ``left/top/right/bottom`` (пиксели исходного кадра). Если координат
        нет, а лицо в кадре ровно одно — берётся оно; если лиц несколько —
        возвращается 409 со списком, чтобы панель показала выбор.
        """
        data = request.get_json(silent=True) or {}
        client_id = str(data.get("client_id") or "")
        name = sanitize_name(data.get("name", ""))
        if not client_id:
            return jsonify({"ok": False, "error": "Не указана камера (client_id)"}), 400
        if not name:
            return jsonify({"ok": False, "error": "Некорректное имя"}), 400
        frame, err = _latest_frame_or_error(client_id)
        if err:
            return err

        box = None
        coords = [data.get(k) for k in ("left", "top", "right", "bottom")]
        if all(isinstance(c, (int, float)) for c in coords):
            left, top, right, bottom = (int(c) for c in coords)
            if right > left and bottom > top:
                box = {"left": left, "top": top, "right": right, "bottom": bottom}

        encoding = None
        if box is None:
            faces, err = _detect_or_error(frame)
            if err:
                return err
            if not faces:
                return jsonify({"ok": False, "error": "В текущем кадре не найдено лиц"}), 422
            if len(faces) > 1:
                entries = store.entries()
                items = []
                for i, (b, enc) in enumerate(faces):
                    match_name, _dist = recog.recognize_face(entries, enc, settings.threshold)
                    items.append({"index": i, **b,
                                  "known": bool(match_name), "name": match_name})
                return jsonify({"ok": False,
                                "error": "В кадре несколько лиц — выберите нужное",
                                "faces": items}), 409
            box, encoding = faces[0]
        else:
            # Рамка пришла из панели (пользователь выбрал лицо в диалоге).
            # Эмбеддинг считаем ПО ИСХОДНОМУ КАДРУ и этой рамке — повторная
            # детекция на обрезке не делается: HOG-детектор ищет лицо вместе с
            # контуром головы/плеч и на плотном кропе почти всегда возвращает
            # 0 лиц, из-за чего фото не сохранялось (ответ 422).
            try:
                encoding = recog.embedding_from_box(frame, box,
                                                    model=settings.detect_model)
            except InferenceBusyError as e:
                log.warning("Добавление лица из кадра отклонено: %s", e)
                return jsonify({"ok": False, "error": str(e), "busy": True}), 503
            except RuntimeError as e:
                return jsonify({"ok": False, "error": str(e)}), 503
            if encoding is None:
                return jsonify({"ok": False,
                                "error": "Не удалось описать выбранное лицо: обновите "
                                         "кадр и выберите лицо заново"}), 422

        # Кроп сохраняем с щедрыми полями: так фото остаётся «узнаваемым» для
        # детектора после перезапуска сервера (store.load() перечитывает файлы).
        crop = recog.crop_face(frame, box["left"], box["top"], box["right"], box["bottom"],
                               margin=CAPTURE_CROP_MARGIN)
        if crop is None:
            return jsonify({"ok": False, "error": "Не удалось вырезать лицо из кадра"}), 422
        jpeg = recog.encode_jpeg(crop, quality=92)
        if jpeg is None:
            return jsonify({"ok": False, "error": "Не удалось подготовить снимок"}), 422
        try:
            ok, message = store.add_photo(name, jpeg, embedding=encoding)
        except InferenceBusyError as e:  # pragma: no cover - защита от гонки
            return jsonify({"ok": False, "error": str(e), "busy": True}), 503
        if not ok:
            log.warning("Не удалось сохранить лицо %s из кадра %s: %s",
                        name, client_id, message)
            status = 503 if "занят" in str(message) else 422
            return jsonify({"ok": False, "error": message}), status
        voice.ensure(name)
        stats.incr("faces_captured")
        events.push("person_added", text=f"{name} (из кадра {client_id})",
                    user=current_user(), source="frame")
        log.info("➕ Лицо добавлено из кадра %s: %s (%s)", client_id, name, message)
        return jsonify({"ok": True, "name": name, "photo": message,
                        "voice": voice.status(name)})

    @app.delete("/api/persons/<path:name>")
    @login_required
    @write_protected
    def api_delete(name):
        removed = store.delete_person(name)
        if not removed:
            return jsonify({"ok": False, "error": "Человек не найден"}), 404
        voice.invalidate(name)
        if throttle is not None:
            throttle.reset(name)
        forget = getattr(stats, "forget_person", None)
        if callable(forget):
            forget(name)   # убираем персональную статистику приветствий
        events.push("person_deleted", text=name, user=current_user())
        return jsonify({"ok": True})

    @app.delete("/api/persons/<path:name>/photos/<path:fname>")
    @login_required
    @write_protected
    def api_delete_photo(name, fname):
        ok, message = store.delete_photo(name, fname)
        if not ok:
            return jsonify({"ok": False, "error": message}), 404
        if not store.has(name):
            voice.invalidate(name)
            if throttle is not None:
                throttle.reset(name)
        events.push("photo_deleted", text=f"{name}/{fname}", user=current_user())
        return jsonify({"ok": True, "photos": store.photos(name)})

    @app.post("/api/persons/<path:name>/rename")
    @login_required
    @write_protected
    def api_rename(name):
        data = request.get_json(silent=True) or request.form.to_dict()
        if not store.has(name):
            return jsonify({"ok": False, "error": "Человек не найден"}), 404
        new_name = sanitize_name(data.get("name", ""))
        if not new_name:
            return jsonify({"ok": False, "error": "Некорректное новое имя"}), 400
        ok, message = store.rename_person(name, new_name)
        if not ok:
            return jsonify({"ok": False, "error": message}), 400
        voice.invalidate(name)
        voice.ensure(new_name)
        if throttle is not None:
            throttle.reset(name)
        renamer = getattr(stats, "rename_person", None)
        if callable(renamer):
            renamer(name, new_name)   # переносим статистику приветствий
        events.push("person_renamed", text=f"{name} → {new_name}", user=current_user())
        return jsonify({"ok": True, "name": new_name, "photos": store.photos(new_name)})

    @app.post("/api/persons/<path:name>/resynthesize")
    @login_required
    @write_protected
    def api_resynth(name):
        if not store.has(name):
            return jsonify({"ok": False, "error": "Человек не найден"}), 404
        info = _variants_info(name)
        voice.ensure(name, force=True)
        return jsonify({"ok": True, "voice": voice.status(name),
                        "phrase": info["texts"][0] if info["texts"]
                                  else format_template(voice.template, name),
                        "phrases": info["texts"], "variants": info["total"]})

    @app.post("/api/persons/<path:name>/greet")
    @login_required
    @write_protected
    def api_greet(name):
        """Ручная отправка приветствия конкретному клиенту (мимо кулдауна)."""
        if not store.has(name):
            return jsonify({"ok": False, "error": "Человек не найден"}), 404
        if sender is None:
            return jsonify({"ok": False, "error": "Отправка команд недоступна"}), 503
        data = request.get_json(silent=True) or {}
        client_id = data.get("client_id")
        if not client_id:
            online = command_targets(hub)
            if not online:
                return jsonify({"ok": False, "error": "Нет клиентов в эфире"}), 409
            client_id = online[0]
        user = current_user() or "admin"

        text, wav = _random_phrase(name)
        if wav is None:
            # Озвучки ещё нет: запускаем синтез и КОРОТКО ждём — команда должна
            # уйти со звуком. Раньше клиент получал greet без аудио и молчал,
            # а досылка озвучки для этой кнопки не была предусмотрена вовсе.
            voice.ensure(name)
            text, wav = _wait_phrase(name, timeout=GREET_WAIT_SECONDS)
        if wav is None and _tts_available():
            threading.Thread(target=_greet_when_ready, args=(name, client_id, user),
                             daemon=True, name="greet-tts").start()
            events.push("greet", name=name, text=text, client_id=client_id,
                        audio="pending", manual=True)
            return jsonify({"ok": True, "client_id": client_id, "voice": "pending",
                            "text": text})

        sender.send(client_id, build_greet_command(name, wav, voice.template, text=text))
        _note_greet(name)
        events.push("greet", name=name, text=text, client_id=client_id,
                    audio=bool(wav), manual=True)
        return jsonify({"ok": True, "client_id": client_id,
                        "voice": "ready" if wav else "unavailable", "text": text})

    def _greet_when_ready(name, client_id, user):
        """Досылает приветствие, как только озвучка будет готова."""
        text, wav = _wait_phrase(name, timeout=120.0)
        if wav is None:
            log.warning("Приветствие для %s не озвучено — команда не отправлена", name)
            events.push("voice_error", name=name, text=text,
                        error="озвучка не готова")
            return
        sender.send(client_id, build_greet_command(name, wav, voice.template, text=text))
        _note_greet(name)
        events.push("greet", name=name, text=text, client_id=client_id,
                    audio=True, manual=True, delayed=True)
        log.info("Приветствие для %s озвучено и отправлено %s (пользователь %s)",
                 name, client_id, user)

    @app.get("/photo/<path:fname>")
    @login_required
    def photo(fname):
        if not fname.lower().endswith(IMAGE_EXTS) or os.path.basename(fname) != fname:
            return jsonify({"ok": False, "error": "Недопустимый тип файла"}), 404
        # send_from_directory() разрешает относительный каталог от корневого
        # пути Flask-приложения (facegate/web/), а не от cwd — с относительным
        # known_faces фото всегда отдавалось 404. Используем абсолютный путь.
        abs_dir = os.path.abspath(store.faces_dir)
        if not os.path.exists(os.path.join(abs_dir, fname)):
            return jsonify({"ok": False, "error": "Файл не найден"}), 404
        return send_from_directory(abs_dir, fname)

    # ---------------- пользователи панели ----------------
    @app.post("/api/auth/password")
    @login_required
    @write_protected
    def api_change_password():
        if not protect:
            return jsonify({"ok": False, "error": "Авторизация отключена (--no-auth)"}), 400
        data = request.get_json(silent=True) or request.form.to_dict()
        ok, message = auth.change_password(current_user(),
                                           data.get("old_password", ""),
                                           data.get("new_password", ""))
        if ok:
            log.info("Пароль администратора %s изменён", current_user())
            events.push("password_changed", user=current_user())
        return jsonify({"ok": ok, "error": None if ok else message}), (200 if ok else 400)

    @app.get("/api/auth/users")
    @login_required
    def api_users():
        if not protect:
            return jsonify({"ok": False, "users": [], "error": "Авторизация отключена"}), 200
        return jsonify({"users": [auth.info(u) for u in auth.users()],
                        "default_credentials": auth.is_default_credentials()})

    @app.post("/api/auth/users")
    @login_required
    @write_protected
    def api_add_user():
        if not protect:
            return jsonify({"ok": False, "error": "Авторизация отключена"}), 400
        data = request.get_json(silent=True) or request.form.to_dict()
        ok, message = auth.add_user(data.get("username", ""), data.get("password", ""))
        return jsonify({"ok": ok, "error": None if ok else message}), (200 if ok else 400)

    @app.delete("/api/auth/users/<path:username>")
    @login_required
    @write_protected
    def api_delete_user(username):
        if not protect:
            return jsonify({"ok": False, "error": "Авторизация отключена"}), 400
        if username == current_user():
            return jsonify({"ok": False, "error": "Нельзя удалить самого себя"}), 400
        ok, message = auth.delete_user(username)
        return jsonify({"ok": ok, "error": None if ok else message}), (200 if ok else 400)

    # ---------------- ошибки ----------------
    @app.errorhandler(404)
    def not_found(_e):
        if is_api():
            return jsonify({"ok": False, "error": "Не найдено"}), 404
        return Response(NOT_FOUND_PAGE, status=404, mimetype="text/html; charset=utf-8")

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({"ok": False, "error": "Файл слишком большой (лимит 25 МБ)",
                        "results": []}), 413

    @app.errorhandler(500)
    def server_error(e):  # pragma: no cover
        log.exception("Ошибка веб-панели: %s", e)
        return jsonify({"ok": False, "error": "Внутренняя ошибка сервера"}), 500

    # служебные ссылки для отладки/тестов
    app.extensions["face_admin"] = {
        "hub": hub, "settings": settings, "stats": stats, "events": events,
        "log_handler": log_handler, "auth": auth, "throttle": throttle,
        "sender": sender, "store": store, "voice": voice, "protect": protect,
        "debug_dir": debug_dir, "soundboard": soundboard,
    }
    return app
