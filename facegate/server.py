# -*- coding: utf-8 -*-
"""Сервер распознавания лиц (headless, без GUI-окон).

Сервер **не создаёт окон**: всё, что видит камера, доступно в админ-панели
в браузере — ``http://<адрес сервера>:8080``.

Устройство (потоки):
    * ``video-receiver``   — принимает кадры по ZeroMQ PULL и сразу публикует
                             их в :class:`facegate.frames.FrameHub` (поэтому
                             видео в браузере плавное, с частотой клиента);
    * ``recognition``      — берёт самый свежий кадр каждого клиента, ищет лица,
                             сравнивает с эталонами, шлёт приветствия и обновляет
                             аннотации (рамки/имена) для панели;
    * ``command-sender``   — владеет PUB-сокетом (:mod:`facegate.commands`);
    * ``web-panel``        — Flask с админ-панелью (:mod:`facegate.web`);
    * ``tts-worker``       — фоновый синтез озвучки (:mod:`facegate.tts`).

Запуск:
    python server.py                      # видео :5555, команды :5556, панель :8080
    python server.py --web-port 9000 --threshold 0.5

Все слои (настройки, кадры, распознавание, TTS, веб) — отдельные модули пакета
``facegate``; этот файл только собирает их вместе и управляет потоками.
"""
import argparse
import json
import logging
import os
import signal
import sys
import threading
import time

import cv2
import numpy as np
import zmq

from facegate import VERSION
from facegate import imaging
from facegate.commands import (CommandSender, build_greet_command,
                               send_command)  # noqa: F401 (ре-экспорт для тестов)
from facegate.config import Settings, split_templates
from facegate.events import EventBus, LogRingHandler
from facegate.frames import FrameHub, translit
from facegate.recognition import engine as recog
from facegate.recognition.engine import (InferenceBusyError, face_engine,
                                         limit_native_threads, recognize_face,
                                         require_face_recognition,
                                         scale_box)  # noqa: F401 (ре-экспорт)
from facegate.recognition.store import FaceStore
from facegate.soundboard import Soundboard
from facegate.stats import Stats, human_uptime
from facegate.throttle import GreetThrottle
from facegate.tts.cache import VoiceCache, format_template
from facegate.web.app import create_app
from facegate.web.auth import AdminAuth

log = logging.getLogger(__name__)

DEFAULT_DEBUG_DIR = "debug_frames"


def _env(name, default=None):
    """Значение переменной окружения (пустая строка считается «не задано»)."""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# ---------- АРГУМЕНТЫ ----------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Сервер распознавания лиц (headless) с админ-панелью в браузере",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--video-port", type=int, default=5555,
                        help="Порт для приёма видео (PULL). Внешние клиенты подключаются к нему")
    parser.add_argument("--command-port", type=int, default=5556,
                        help="Порт для отправки команд (PUB)")
    parser.add_argument("--known-faces-dir", type=str, default="known_faces",
                        help="Папка с эталонными фото")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Порог схожести (0.05-1.0). По умолчанию берётся из настроек панели")
    parser.add_argument("--web-host", type=str, default=_env("WEB_HOST", "0.0.0.0"),
                        help="Адрес, на котором слушает веб-панель")
    parser.add_argument("--web-port", type=int, default=8080,
                        help="Порт веб-панели (админ-панель)")
    parser.add_argument("--no-web", action="store_true",
                        help="Отключить веб-панель (только ZMQ)")
    parser.add_argument("--greet-cooldown", type=float, default=None,
                        help="Минимальный интервал между приветствиями одного человека, сек")
    parser.add_argument("--greet-template", type=str, default=None,
                        help="[совместимость] Один шаблон фразы приветствия "
                             "({name} — имя человека). Лучше --greet-templates")
    parser.add_argument("--greet-templates", type=str, default=None,
                        help="Пул фраз приветствия: строки разделены переводом строки "
                             "или последовательностью \\n. Для каждой фразы "
                             "синтезируется свой WAV, при распознавании фраза "
                             "выбирается случайно без повторов подряд")
    parser.add_argument("--voice-cache-dir", type=str, default="voice_cache",
                        help="Папка кэша синтезированных фраз")
    # --- озвучка (TTS) ---
    parser.add_argument("--tts-engine", type=str,
                        default=_env("TTS_ENGINE", None),
                        choices=["auto", "silero", "espeak", "pyttsx3"],
                        help="Движок озвучки: silero — нейросетевой русский голос "
                             "(в Docker по умолчанию), espeak — espeak-ng, "
                             "pyttsx3 — SAPI5/NSSpeech, auto — первый доступный. "
                             "Переменная окружения: TTS_ENGINE")
    parser.add_argument("--tts-rate", type=int, default=None,
                        help="Скорость речи для espeak/pyttsx3 (слов в минуту)")
    parser.add_argument("--tts-voice", type=str, default=_env("TTS_VOICE", None),
                        help="Голос TTS: для silero — baya/kseniya/xenia/eugene/random, "
                             "для espeak — например ru. Переменная окружения: TTS_VOICE")
    parser.add_argument("--tts-speed", type=float, default=None,
                        help="Скорость речи silero (0.5–2.0). Переменная окружения: TTS_SPEED")
    # --- админ-панель: доступ ---
    parser.add_argument("--admin-user", type=str, default=None,
                        help="Логин админ-панели (по умолчанию admin, либо ADMIN_USER)")
    parser.add_argument("--admin-password", type=str, default=None,
                        help="Пароль админ-панели (по умолчанию admin, либо ADMIN_PASSWORD)")
    parser.add_argument("--admin-data-dir", type=str, default="admin_data",
                        help="Папка с учётными данными, ключом сессий и настройками")
    parser.add_argument("--no-auth", action="store_true",
                        help="Отключить вход в панель (не рекомендуется, только для отладки)")
    # --- видео/трансляция ---
    parser.add_argument("--jpeg-quality", type=int, default=None,
                        help="Качество JPEG для трансляции в панель (25-95)")
    parser.add_argument("--stream-fps", type=float, default=None,
                        help="Максимальная частота кадров трансляции в панель")
    parser.add_argument("--process-width", type=int, default=None,
                        help="Ширина кадра для анализа (0 = без уменьшения)")
    parser.add_argument("--detect-model", type=str, default=None, choices=["hog", "cnn"],
                        help="Модель поиска лиц: hog (CPU) или cnn (GPU)")
    parser.add_argument("--no-recognition", action="store_true",
                        help="Только транслировать видео, лица не распознавать")
    # --- саундборд ---
    parser.add_argument("--soundboard-dir", type=str,
                        default=_env("SOUNDBOARD_DIR", "soundboard"),
                        help="Папка загруженных звуков для вкладки «Саундборд»")
    # --- совместимость со старыми запусками ---
    parser.add_argument("--no-debug", action="store_true",
                        help="[устарел] сервер всегда работает без окон OpenCV; "
                             "видео смотрите в админ-панели")
    parser.add_argument("--save-debug", action="store_true",
                        help="Сохранять аннотированные кадры в папку debug_frames "
                             "(по кнопке в панели и при каждом приветствии)")
    parser.add_argument("--debug-dir", type=str, default=DEFAULT_DEBUG_DIR,
                        help="Папка для снимков")
    parser.add_argument("--log-file", type=str, default=None,
                        help="Писать лог также в файл")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Уровень логирования")
    parser.add_argument("--stats-interval", type=float, default=60.0,
                        help="Как часто печатать сводку статистики в лог (0 = не печатать)")
    parser.add_argument("--version", action="version", version=f"facegate-server {VERSION}")
    args = parser.parse_args(argv)
    if args.no_debug:
        # Флаг оставлен для совместимости со старыми docker-compose/скриптами.
        pass
    if args.tts_speed is None:
        env_speed = _env("TTS_SPEED")
        if env_speed:
            try:
                args.tts_speed = float(env_speed)
            except ValueError:
                log.warning("TTS_SPEED=%r — не число, игнорирую", env_speed)
    return args


def setup_logging(level="INFO", log_file=None, ring_handler=None):
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    if ring_handler is not None:
        handlers.append(ring_handler)
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        handlers=handlers, force=True)


class LatestFrames:
    """По самому свежему кадру на каждого клиента (старые кадры отбрасываются)."""

    def __init__(self):
        self._cond = threading.Condition()
        self._frames = {}
        self._wakeup = False

    def put(self, client_id, frame):
        with self._cond:
            self._frames[client_id] = frame
            self._cond.notify()

    def take_all(self, timeout=0.25):
        """Блокирует до появления кадра и забирает сразу все свежие кадры.

        Возвращает пустой словарь по таймауту или после :meth:`wake`.
        """
        deadline = time.time() + timeout
        with self._cond:
            while not self._frames and not self._wakeup:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return {}
                self._cond.wait(remaining)
            frames, self._frames = self._frames, {}
            self._wakeup = False
            return frames

    def wake(self):
        """Немедленно будит ожидающий поток распознавания (используется при остановке)."""
        with self._cond:
            self._wakeup = True
            self._cond.notify_all()

    def __len__(self):
        with self._cond:
            return len(self._frames)


# ---------- СЕРВЕР ----------
class RecognitionServer:
    """Собирает все компоненты и управляет потоками."""

    def __init__(self, args):
        self.args = args
        # Все рабочие каталоги приводим к абсолютным путям СРАЗУ.
        # Flask (send_file / send_from_directory) разрешает относительные пути
        # от каталога модуля приложения (facegate/web/), а не от cwd процесса,
        # из-за чего фото и звуки «исчезали» (404/500), хотя os.path.exists
        # от корня проекта проходил успешно.
        for _attr in ("known_faces_dir", "voice_cache_dir", "soundboard_dir",
                      "debug_dir", "admin_data_dir"):
            _value = getattr(args, _attr, None)
            if _value:
                setattr(args, _attr, os.path.abspath(_value))
        # Пул фраз приветствия: --greet-templates (несколько строк) приоритетнее
        # устаревшего --greet-template.
        greet_pool = split_templates(getattr(args, "greet_templates", None))
        if not greet_pool and getattr(args, "greet_template", None):
            greet_pool = split_templates(args.greet_template)
        greet_pool = "\n".join(greet_pool) if greet_pool else None
        self.stop_event = threading.Event()
        self.log_ring = LogRingHandler(maxlen=1000)
        self.events = EventBus(maxlen=500)
        self.stats = Stats()

        # Настройки: файл в admin_data + переопределения из аргументов командной строки
        admin_dir = args.admin_data_dir
        self.settings = Settings(
            path=os.path.join(admin_dir, "settings.json") if admin_dir else None,
            threshold=args.threshold,
            greet_cooldown=args.greet_cooldown,
            greet_template=args.greet_template,
            greet_templates=greet_pool,
            tts_rate=args.tts_rate,
            tts_voice=args.tts_voice,
            tts_engine=args.tts_engine,
            tts_speed=args.tts_speed,
            jpeg_quality=args.jpeg_quality,
            stream_fps=args.stream_fps,
            process_width=args.process_width,
            detect_model=args.detect_model,
            recognition_enabled=(not args.no_recognition),
        )

        cli_overrides = {k: v for k, v in (
            ("threshold", args.threshold), ("greet_cooldown", args.greet_cooldown),
            ("greet_templates", greet_pool), ("tts_rate", args.tts_rate),
            ("tts_voice", args.tts_voice), ("tts_engine", args.tts_engine),
            ("tts_speed", args.tts_speed), ("jpeg_quality", args.jpeg_quality),
            ("stream_fps", args.stream_fps), ("process_width", args.process_width),
            ("detect_model", args.detect_model),
        ) if v is not None}
        if args.no_recognition:
            cli_overrides["recognition_enabled"] = False
        if cli_overrides:
            self.settings.update(cli_overrides, save=True)

        self.hub = FrameHub(jpeg_quality=self.settings.jpeg_quality,
                            stream_fps=self.settings.stream_fps,
                            stale_after=self.settings.stale_after)
        self.store = FaceStore(args.known_faces_dir)
        self.voice = VoiceCache(cache_dir=args.voice_cache_dir,
                                templates=self.settings.greet_template_list,
                                template=self.settings.greet_template,
                                rate=self.settings.tts_rate,
                                voice_hint=self.settings.tts_voice,
                                engine=self.settings.tts_engine,
                                speed=self.settings.tts_speed)
        self.soundboard = Soundboard(
            sounds_dir=args.soundboard_dir,
            state_path=os.path.join(admin_dir, "soundboard.json") if admin_dir else None,
            voice=self.voice)
        self.throttle = GreetThrottle(self.settings.greet_cooldown)
        self.sender = CommandSender(port=args.command_port)
        self.pending = LatestFrames()
        self.app = None
        self.web_thread = None
        self.auth = None
        self._video_ready = threading.Event()
        self._video_port_bound = None
        self._last_unknown_event = {}
        self._context = None

    # ---------- запуск ----------
    def start(self):
        args = self.args
        setup_logging(args.log_level, args.log_file, self.log_ring)
        # Ограничиваем нативные пулы потоков dlib/OpenMP: параллельный OpenMP
        # внутри dlib — вторая типовая причина зависаний процесса.
        limit_native_threads()
        log.info("Сервер распознавания лиц v%s (headless, без окон OpenCV)", VERSION)
        log.info("Фраз приветствия в пуле: %d", len(self.settings.greet_template_list))

        persons = self.store.load()
        log.info("Эталонов: %d чел. / %d фото (папка %s)",
                 len(persons), self.store.total_photos(), args.known_faces_dir)

        # Прогрев кэша озвучки
        for name in self.store.names():
            self.voice.ensure(name)

        if args.save_debug:
            os.makedirs(args.debug_dir, exist_ok=True)
            log.info("Снимки с разметкой будут сохраняться в %s", args.debug_dir)

        self.sender.start()

        self.receiver_thread = threading.Thread(target=self._receiver_loop,
                                                name="video-receiver", daemon=True)
        self.receiver_thread.start()
        self.recognition_thread = threading.Thread(target=self._recognition_loop,
                                                   name="recognition", daemon=True)
        self.recognition_thread.start()
        self.monitor_thread = threading.Thread(target=self._monitor_loop,
                                               name="monitor", daemon=True)
        self.monitor_thread.start()

        if not self._video_ready.wait(timeout=10.0):
            log.error("Не удалось поднять PULL-сокет на порту %s", args.video_port)

        if not args.no_web:
            self._start_web()

        log.info("Приём видео: tcp://*:%s | команды: tcp://*:%s",
                 self._video_port_bound or args.video_port, args.command_port)
        if not args.no_web:
            log.info("🌐 Админ-панель: http://%s:%s  (всё, что видит камера, — там)",
                     "localhost" if args.web_host in ("0.0.0.0", "") else args.web_host,
                     args.web_port)
        return self

    def _start_web(self):
        args = self.args
        auth = AdminAuth(data_dir=args.admin_data_dir,
                         username=args.admin_user,
                         password=args.admin_password,
                         enabled=not args.no_auth)
        self.auth = auth
        self.app = create_app(
            self.store, self.voice, args.known_faces_dir,
            hub=self.hub, settings=self.settings, stats=self.stats,
            events=self.events, log_handler=self.log_ring, auth=auth,
            throttle=self.throttle, sender=self.sender,
            debug_dir=args.debug_dir, protect=auth.enabled,
            soundboard=self.soundboard)
        self.web_thread = threading.Thread(
            target=self._run_web, daemon=True, name="web-panel")
        self.web_thread.start()

    def _run_web(self):
        try:
            self.app.run(host=self.args.web_host, port=self.args.web_port,
                         threaded=True, debug=False, use_reloader=False)
        except Exception as e:  # pragma: no cover
            log.error("Веб-панель остановилась: %s", e)

    # ---------- поток приёма видео ----------
    def _receiver_loop(self):
        context = zmq.Context()
        self._context = context
        socket = context.socket(zmq.PULL)
        try:
            socket.setsockopt(zmq.RCVHWM, 10)
            socket.setsockopt(zmq.RCVTIMEO, 250)
            socket.setsockopt(zmq.LINGER, 0)
            socket.bind(f"tcp://*:{self.args.video_port}")
            self._video_port_bound = self.args.video_port
        except zmq.ZMQError as e:
            log.error("Не удалось занять порт %s: %s", self.args.video_port, e)
            self._video_ready.set()
            return
        self._video_ready.set()
        log.info("Приём видео запущен на порту %s", self.args.video_port)

        while not self.stop_event.is_set():
            try:
                parts = socket.recv_multipart()
            except zmq.Again:
                continue
            except zmq.ZMQError as e:
                if self.stop_event.is_set():
                    break
                log.error("Ошибка приёма видео: %s", e)
                time.sleep(0.05)
                continue
            self._handle_multipart(parts)

        socket.close(0)
        context.term()
        log.info("Поток приёма видео остановлен")

    def _handle_multipart(self, parts):
        """Кадр = [client_id, jpeg] или [client_id, jpeg, meta_json]."""
        if len(parts) < 2:
            self.stats.incr("decode_errors")
            return
        try:
            client_id = parts[0].decode("utf-8")
        except UnicodeDecodeError:
            client_id = repr(parts[0][:24])
        meta = None
        if len(parts) >= 3:
            try:
                meta = json.loads(parts[2].decode("utf-8"))
                if not isinstance(meta, dict):
                    meta = None
            except Exception:
                meta = None
        frame = cv2.imdecode(np.frombuffer(parts[1], dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            self.stats.incr("decode_errors")
            log.warning("Не удалось декодировать кадр от %s (%d байт)", client_id, len(parts[1]))
            return
        self.stats.incr("frames_received")
        # публикуем СРАЗУ — видео в панели не зависит от скорости распознавания
        self.hub.publish_raw(client_id, frame, meta)
        self.pending.put(client_id, frame)

    # ---------- поток распознавания ----------
    def _recognition_loop(self):
        log.info("Поток распознавания запущен")
        while not self.stop_event.is_set():
            batch = self.pending.take_all(timeout=0.25)
            for client_id, frame in batch.items():
                if self.stop_event.is_set():
                    break
                started = time.time()
                try:
                    self.process_frame(client_id, frame)
                except InferenceBusyError as e:  # pragma: no cover - timeout=0
                    log.warning("Кадр от %s пропущен: %s", client_id, e)
                except Exception as e:
                    log.exception("Ошибка обработки кадра от %s: %s", client_id, e)
                elapsed = time.time() - started
                if elapsed > 5.0:
                    log.warning("Обработка кадра от %s заняла %.1f с — проверьте "
                                "process_width/detect_model и загрузку CPU",
                                client_id, elapsed)
        log.info("Поток распознавания остановлен")

    def process_frame(self, client_id, frame):
        """Распознавание одного кадра + публикация аннотаций в панель."""
        settings = self.settings
        annotated = frame
        annotations = []
        faces = recognized = unknown = 0

        if settings.recognition_enabled:
            face_engine()
            proc, factor = recog.downscale_for_analysis(frame, settings.process_width)
            rgb = cv2.cvtColor(proc, cv2.COLOR_BGR2RGB)
            # Одна блокировка на детекцию + эмбеддинги: параллельный вызов dlib
            # из HTTP-воркера панели (добавление лица из кадра) больше не может
            # привести к дедлоку OpenMP и зависанию всего сервера.
            # timeout=0 — поток распознавания ждёт освобождения движка сколько нужно.
            locations, encodings = recog.detect_with_embeddings(
                rgb, model=settings.detect_model, timeout=0, polite=True)
            entries = self.store.entries()
            annotated = frame.copy()
            for box, enc in zip(locations, encodings):
                top, right, bottom, left = scale_box(box, factor)
                name, distance = recognize_face(entries, enc, settings.threshold)
                known = bool(name)
                faces += 1
                if known:
                    recognized += 1
                    color = (0, 255, 0)
                    label = f"{name} {distance:.3f}"
                else:
                    unknown += 1
                    color = (0, 0, 255)
                    label = f"Unknown {distance:.3f}"
                # рамка рисуется ДО приветствия, чтобы снимки (--save-debug)
                # и кадры из панели уже были с разметкой
                cv2.rectangle(annotated, (left, top), (right, bottom), color, 2)
                if settings.draw_labels:
                    cv2.putText(annotated, translit(label), (left, max(15, top - 8)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                if known:
                    self._maybe_greet(client_id, name, distance, annotated,
                                      (left, top, right, bottom))
                else:
                    self._note_unknown(client_id, distance)
                annotations.append({
                    "left": left, "top": top, "right": right, "bottom": bottom,
                    "name": name, "distance": round(distance, 4), "known": known,
                    "label": label if settings.draw_labels else "",
                })

        self.hub.publish_annotation_result(client_id, annotated, annotations,
                                           faces=faces, recognized=recognized,
                                           unknown=unknown)
        self.stats.incr("frames_processed")
        self.stats.incr("faces_detected", faces)
        self.stats.incr("recognized", recognized)
        self.stats.incr("unknown", unknown)

    def _maybe_greet(self, client_id, name, distance, annotated, box):
        settings = self.settings
        if not settings.greeting_enabled:
            return
        if not self.throttle.allow(name):
            self.stats.incr("greets_throttled")
            self.events.push("throttled", name=name, client_id=client_id,
                             distance=round(distance, 4),
                             wait=self.throttle.wait_left(name))
            return
        # Случайная фраза из пула (без повторения предыдущей) + её готовый WAV
        text, wav = self._phrase_for(name)
        if wav is None:
            # кэш ещё не готов — отправляем текст и ставим синтез в очередь
            self.voice.ensure(name)
            self.stats.incr("greets_no_audio")
            log.warning("Нет кэшированной озвучки для %s, синтез запланирован", name)
        payload = build_greet_command(name, wav, settings.greet_template, text=text)
        self.sender.send(client_id, payload)
        self.stats.incr_greet(name)   # общий счётчик + статистика по человеку
        self.stats.incr("commands_sent")
        log.info("✅ Распознан %s (dist=%.3f) → %s%s: «%s»", name, distance, client_id,
                 " с аудио" if wav else " без аудио", text)
        self.events.push("greet", name=name, text=text, client_id=client_id,
                         distance=round(distance, 4), audio=bool(wav))
        if self.args.save_debug:
            # рамка уже нарисована в process_frame — сохраняем кадр как есть.
            # Запись через imencode + байты: cv2.imwrite молча не создаёт файл,
            # если в пути есть кириллица (Windows), — снимки «пропадали».
            stamp = time.strftime("%Y%m%d_%H%M%S")
            safe = imaging.ascii_safe_name(name, max_len=30, fallback="person")
            path = os.path.join(self.args.debug_dir, f"greet_{safe}_{stamp}.jpg")
            ok, message = imaging.write_image(path, annotated,
                                              quality=self.settings.jpeg_quality)
            if not ok:
                log.error("Не удалось сохранить снимок приветствия: %s", message)

    def _phrase_for(self, name):
        """(текст, WAV|None) случайной фразы приветствия из пула вариантов."""
        picker = getattr(self.voice, "get_random", None)
        if callable(picker):
            try:
                text, wav = picker(name)
                if text:
                    return text, wav
            except Exception as e:  # pragma: no cover - кэш не должен ронять сервер
                log.warning("Не удалось выбрать вариант озвучки для %s: %s", name, e)
        return format_template(self.settings.greet_template, name), self.voice.get(name)

    def _note_unknown(self, client_id, distance):
        """Не спамим событиями: одно «неизвестное лицо» на клиента не чаще раза в 3 сек."""
        now = time.time()
        last = self._last_unknown_event.get(client_id, 0.0)
        if now - last < 3.0:
            return
        self._last_unknown_event[client_id] = now
        log.info("❌ Неизвестное лицо (dist=%.3f) от %s", distance, client_id)
        self.events.push("unknown", client_id=client_id, distance=round(distance, 4))

    # ---------- мониторинг ----------
    def _monitor_loop(self):
        seen_offline = set()
        last_stats = time.time()
        while not self.stop_event.is_set():
            time.sleep(2.0)
            infos = self.hub.clients_info()
            online = {i["client_id"] for i in infos if i["online"]}
            for info in infos:
                cid = info["client_id"]
                if not info["online"] and cid not in seen_offline:
                    seen_offline.add(cid)
                    log.warning("Клиент %s перестал присылать видео", cid)
                    self.events.push("client_gone", client_id=cid,
                                     text="нет видео больше "
                                          f"{self.settings.stale_after:.0f} с")
                elif info["online"] and cid in seen_offline:
                    seen_offline.discard(cid)
                    log.info("Клиент %s снова в эфире", cid)
                    self.events.push("client_back", client_id=cid)
            if self.args.stats_interval and time.time() - last_stats >= self.args.stats_interval:
                last_stats = time.time()
                c = self.stats.counters()
                t = self.hub.totals()
                log.info("Сводка: аптайм %s | клиентов онлайн %d/%d | "
                         "лиц в кадре сейчас %d | кадров получено всего %d | "
                         "приветствий всего %d | неизвестных %d",
                         human_uptime(time.time() - self.stats.started_at),
                         t["clients_online"], t["clients_total"],
                         t["current_faces"], c["frames_received"],
                         c["greets_sent"], c["unknown"])

    # ---------- остановка ----------
    def shutdown(self, signum=None, _frame=None):
        if self.stop_event.is_set():
            return
        if signum:
            log.info("Получен сигнал %s — останавливаемся", signum)
        self.stop_event.set()
        self.pending.wake()
        self.sender.stop(timeout=2.0)
        self.voice.stop(timeout=3.0)
        for thread in (getattr(self, "receiver_thread", None),
                       getattr(self, "recognition_thread", None),
                       getattr(self, "monitor_thread", None)):
            if thread and thread.is_alive():
                thread.join(timeout=3.0)
        log.info("Сервер остановлен")

    def wait(self):
        """Блокирует основной поток до сигнала остановки."""
        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(0.5)
        except KeyboardInterrupt:
            log.info("Остановка по Ctrl+C")
            self.shutdown()


def start_server(args=None):
    """Точка входа: собирает сервер, ставит обработчики сигналов и работает."""
    args = args or parse_args()
    server = RecognitionServer(args)
    try:
        signal.signal(signal.SIGTERM, server.shutdown)
        signal.signal(signal.SIGINT, server.shutdown)
    except (ValueError, AttributeError):  # pragma: no cover - не главный поток
        pass
    server.start()
    try:
        server.wait()
    except KeyboardInterrupt:
        server.shutdown()
    return server


def main(argv=None):
    start_server(parse_args(argv))


if __name__ == "__main__":
    main()
