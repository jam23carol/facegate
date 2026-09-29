# -*- coding: utf-8 -*-
"""Диагностика окружения: что установлено, что сломано и как починить.

    python tools/check_env.py             # быстрая проверка зависимостей
    python tools/check_env.py --client    # только то, что нужно клиенту
    python tools/check_env.py --audio     # проверить звуковой тракт клиента
    python tools/check_env.py --camera    # ещё и проверить доступные камеры
    python tools/check_env.py --ports     # ещё и проверить, свободны ли порты

Скрипт ничего не устанавливает — только печатает состояние и точные команды
для починки. Код возврата 0 = всё в порядке, 1 = есть критичные проблемы.

Отдельно проверяются вещи, из-за которых «всё молча не работало»:
  * виртуальное окружение (install.sh/install.bat создают его всегда);
  * права на запись в каталоги данных (known_faces/, debug_frames/) — при
    запуске в Docker от root/чужого PUID снимки и фото не сохранялись;
  * аудиоплеер клиента и приведение 24 кГц → 48 кГц (без него озвучка
    в Windows не играла вовсе, а в Linux звучала вдвое быстрее).
"""
import argparse
import importlib
import importlib.util
import os
import platform
import shutil
import socket
import sys

OK, INFO, WARN, FAIL = "OK  ", "INFO", "WARN", "FAIL"
_problems = []
_warnings = []


def line(status, title, detail="", hint=""):
    print(f"[{status}] {title:<34} {detail}")
    if hint and status not in (OK, INFO):
        print(f"       └─ {hint}")
    if status == FAIL:
        _problems.append(f"{title}: {detail} {hint}".strip())
    elif status == WARN:
        _warnings.append(f"{title}: {detail} {hint}".strip())


def try_import(name, pkg=None):
    try:
        mod = importlib.import_module(pkg or name)
        return mod, None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def check_module(title, name, hint, pkg=None):
    mod, err = try_import(name, pkg)
    if mod is None:
        line(FAIL, title, err, hint)
        return None
    version = None
    try:  # сначала метаданные дистрибутива: __version__ у flask/click устарел
        from importlib.metadata import version as _v
        version = _v(pkg or name)
    except Exception:  # noqa: BLE001
        version = getattr(mod, "__version__", None) or "?"
    line(OK, title, f"v{version}")
    return mod


def check_tts():
    """Движки озвучки: Silero (нейросетевой русский) → espeak-ng → pyttsx3."""
    print("-" * 78)
    have_silero = (importlib.util.find_spec("silero") is not None
                   and importlib.util.find_spec("torch") is not None
                   and importlib.util.find_spec("scipy") is not None)
    if have_silero:
        line(OK, "TTS: silero (нейросетевой)", "пакеты silero, torch и scipy установлены",
             "")
        try:
            import silero as _silero_pkg
            model_dir = os.path.join(os.path.dirname(_silero_pkg.__file__), "model")
            cached = os.path.isdir(model_dir) and any(
                n.endswith((".pt", ".jit")) for n in os.listdir(model_dir))
            if cached:
                line(OK, "TTS: модель silero", f"закэширована в {model_dir}")
            else:
                line(WARN, "TTS: модель silero", "не найдена в кэше",
                     "при первом синтезе модель скачается (нужен интернет): "
                     "python -c \"from silero import silero_tts; "
                     "silero_tts(language='ru', speaker='v5_ru')\"")
        except Exception as e:  # noqa: BLE001
            line(WARN, "TTS: модель silero", f"{type(e).__name__}: {e}")
    else:
        line(INFO, "TTS: silero (нейросетевой)", "не установлен",
             "качественная русская озвучка: ./install.sh --tts  "
             "(в Docker-образе сервера уже есть)")

    if sys.platform.startswith("win"):
        pyttsx3 = importlib.util.find_spec("pyttsx3") is not None
        line(OK if pyttsx3 else WARN, "TTS: SAPI5 (Windows)",
             "доступен через pyttsx3" if pyttsx3 else "pyttsx3 не установлен",
             "" if pyttsx3 else "pip install \"pyttsx3>=2.90\"")
    elif sys.platform == "darwin":
        line(OK if shutil.which("say") else WARN, "TTS: NSSpeech/say (macOS)",
             "доступен" if shutil.which("say") else "не найден")
        engine = shutil.which("espeak-ng") or shutil.which("espeak")
        if engine:
            line(OK, "TTS: espeak-ng (запасной)", os.path.basename(engine))
    else:
        engine = shutil.which("espeak-ng") or shutil.which("espeak")
        if engine:
            line(OK, "TTS: espeak-ng (запасной)", os.path.basename(engine))
        else:
            line(WARN, "TTS: espeak-ng (запасной)", "не найден",
                 "sudo apt install espeak-ng  (без него и без silero озвучка "
                 "будет приходить только текстом)")

    if shutil.which("ffmpeg"):
        line(OK, "ffmpeg (саундборд)", "конвертация mp3/ogg → WAV доступна")
    else:
        line(INFO, "ffmpeg (саундборд)", "не установлен",
             "нужен только для трансляции не-WAV звуков саундборда клиентам: "
             "sudo apt install ffmpeg (в Docker-образе сервера уже есть)")


def check_audio(root):
    """Звуковой тракт клиента: плеер, ресемплинг 24 кГц → 48 кГц, очередь."""
    print("-" * 78)
    print("АУДИО КЛИЕНТА")
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import client  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        line(FAIL, "client.py", f"{type(e).__name__}: {e}",
             "запускайте из корня проекта")
        return
    player = client.resolve_player(None)
    candidates = client.player_candidates(None)
    if player:
        line(OK, "аудиоплеер", " ".join(player))
        line(INFO, "кандидаты", ", ".join(" ".join(c) for c in candidates[:6]))
    else:
        line(FAIL, "аудиоплеер", "не найден", client.player_hint())

    import io  # noqa: PLC0415
    import wave  # noqa: PLC0415
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x10" * 24000)     # 1 секунда 24 кГц (формат Silero)
    data, info = client.normalize_wav(buf.getvalue(), target_rate=48000)
    if info.get("error"):
        line(FAIL, "нормализация WAV", info["error"])
        return
    with wave.open(io.BytesIO(data)) as w:
        out_rate = w.getframerate()
        seconds = w.getnframes() / float(out_rate)
    if out_rate == 48000 and 1.0 < seconds < 1.4:
        line(OK, "ресемплинг 24 кГц → 48 кГц", f"{seconds:.2f} c, моно 16 бит")
    else:
        line(FAIL, "ресемплинг 24 кГц → 48 кГц",
             f"получилось {out_rate} Гц / {seconds:.2f} c",
             "без передискретизации aplay играет фразу вдвое быстрее")
    line(INFO, "очередь воспроизведения",
         "звуки играются последовательно и не обрывают друг друга")
    line(INFO, "проверка на машине клиента",
         "python client.py --check-audio  (играет тестовый сигнал)")


def check_writable(root, dirname):
    """Проверяет, что в каталог данных реально можно писать (пробный файл)."""
    path = os.path.join(root, dirname)
    if not os.path.isdir(path):
        return
    probe = os.path.join(path, ".write_probe")
    try:
        with open(probe, "wb") as f:
            f.write(b"ok")
        os.remove(probe)
        line(OK, f"запись в {dirname}/", "разрешена")
    except OSError as e:
        line(FAIL, f"запись в {dirname}/", f"{type(e).__name__}: {e}",
             f"снимки/фото не сохраняются. Исправьте права: sudo chown -R "
             f"$(id -u):$(id -g) {path}  (в Docker — PUID/PGID вашего пользователя)")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Проверка окружения")
    parser.add_argument("--camera", action="store_true", help="Проверить камеры (0..5)")
    parser.add_argument("--ports", action="store_true", help="Проверить порты 5555/5556/8080")
    parser.add_argument("--audio", action="store_true",
                        help="Проверить звуковой тракт клиента (плеер, ресемплинг)")
    parser.add_argument("--client", action="store_true",
                        help="Проверять только зависимости клиента "
                             "(opencv/pyzmq/numpy + звук), без dlib/flask/TTS")
    parser.add_argument("--quiet", action="store_true", help="Только итог")
    args = parser.parse_args(argv)

    print("=" * 78)
    print("Проверка окружения: FaceGate — система распознавания лиц")
    print("=" * 78)
    print(f"Python   : {sys.version.split()[0]} ({platform.python_implementation()})")
    print(f"Платформа: {platform.system()} {platform.release()} {platform.machine()}")
    print(f"Интерпр. : {sys.executable}")
    in_venv = sys.prefix != sys.base_prefix
    print(f"venv     : {'да' if in_venv else 'нет'}")
    print("-" * 78)
    if not in_venv:
        line(WARN, "виртуальное окружение", "не активно",
             "install.sh / install.bat создают .venv автоматически; запуск — "
             "./run-server.sh или run-server.bat")

    # --- базовые ---
    numpy = check_module("numpy", "numpy", "pip install \"numpy>=1.24\"")
    cv2 = check_module("opencv", "cv2",
                       "pip install \"opencv-python>=4.5.0\" (для сервера без GUI — "
                       "opencv-python-headless)")
    check_module("pyzmq", "zmq", "pip install \"pyzmq>=22.0.0\"")

    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if args.client or args.audio:
        check_audio(root_dir)
    if args.client:
        for dirname in ("client_frames",):
            check_writable(root_dir, dirname)
        print("=" * 78)
        return finish()
    check_module("flask (админ-панель)", "flask", "pip install \"flask>=2.2.0\"")
    check_module("Pillow", "PIL", "pip install \"Pillow>=9.0\"", pkg="PIL")

    # --- распознавание лиц ---
    print("-" * 78)
    check_module("dlib", "dlib",
                 "Быстрый путь: pip install \"dlib-bin>=19.24\"   |   "
                 "или: pip install --force-reinstall \"setuptools<70\" && "
                 "pip install \"dlib>=19.24\" (нужны cmake и компилятор)")
    models, err = try_import("face_recognition_models")
    if models is None:
        line(FAIL, "face_recognition_models", err,
             "pip install \"face_recognition_models>=0.3.0\"  (пакет есть на PyPI, "
             "git не нужен; без него face_recognition не импортируется)")
    else:
        data_dir = getattr(models, "__file__", "")
        size = 0
        try:
            root = os.path.dirname(data_dir)
            for dirpath, _dirs, files in os.walk(root):
                for f in files:
                    if f.endswith(".dat"):
                        size += os.path.getsize(os.path.join(dirpath, f))
        except OSError:
            pass
        status = OK if size > 1_000_000 else WARN
        line(status, "face_recognition_models", f"веса моделей: {size / 1e6:.1f} МБ",
             "похоже, модели не скачались — переустановите пакет")

    fr = check_module("face_recognition", "face_recognition",
                      "pip install --no-deps \"face_recognition>=1.3.0\" "
                      "(если dlib уже стоит через dlib-bin)")

    # --- живой самопроверочный вызов ---
    if fr is not None and numpy is not None:
        try:
            import numpy as _np
            img = _np.zeros((120, 120, 3), dtype=_np.uint8)
            locations = fr.face_locations(img, model="hog")
            line(OK, "self-test face_locations", f"лиц найдено: {len(locations)} (на пустом кадре)")
        except Exception as e:  # noqa: BLE001
            line(FAIL, "self-test face_locations", f"{type(e).__name__}: {e}",
                 "обычно это битые/недостающие веса моделей: переустановите "
                 "face_recognition_models")

    # --- OpenCV GUI (нужен только клиенту) ---
    if cv2 is not None:
        headless = "headless" in (getattr(cv2, "getBuildInformation", lambda: "")() or "")
        has_gui = hasattr(cv2, "imshow")
        if has_gui and not headless:
            line(OK, "opencv GUI (окно клиента)", "imshow доступен")
        elif has_gui:
            line(WARN, "opencv GUI (окно клиента)", "собран без GUI (headless)",
                 "на клиенте нужна сборка с GUI: pip install opencv-python; "
                 "на сервере GUI не нужен вовсе")
        else:
            line(WARN, "opencv GUI (окно клиента)", "imshow недоступен",
                 "клиент запускайте с --no-debug, либо поставьте opencv-python")

    # --- озвучка (TTS) ---
    check_tts()

    # --- звук клиента (по запросу или при полной проверке) ---
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if args.audio:
        check_audio(root_dir)

    # --- порты ---
    if args.ports:
        print("-" * 78)
        for port in (5555, 5556, 8080):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("0.0.0.0", port))
                    line(OK, f"порт {port}", "свободен")
                except OSError as e:
                    line(WARN, f"порт {port}", f"занят ({e})",
                         "запустите сервер с другим портом, например "
                         f"--web-port {port + 100}")

    # --- камеры и права /dev/video* ---
    if args.camera and cv2 is not None:
        print("-" * 78)
        found = 0
        for idx in range(6):
            cap = cv2.VideoCapture(idx)
            if cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    h, w = frame.shape[:2]
                    line(OK, f"камера {idx}", f"{w}x{h}")
                    found += 1
                else:
                    line(WARN, f"камера {idx}", "открывается, но кадры не читает")
            else:
                line(OK, f"камера {idx}", "нет устройства")
            cap.release()
        if not found:
            line(WARN, "камеры", "ни одной рабочей камеры не найдено",
                 "проверьте /dev/video* и права доступа (нужна группа video: "
                 "sudo usermod -aG video $USER, в Docker — group_add: video); "
                 "сервер работает и без камеры — видео присылают клиенты")

    # --- каталоги проекта ---
    print("-" * 78)
    root = root_dir
    for dirname, purpose in (("known_faces", "эталоны лиц"),
                             ("voice_cache", "кэш озвучки"),
                             ("admin_data", "учётные данные и настройки панели"),
                             ("debug_frames", "снимки с разметкой"),
                             ("soundboard", "звуки для вкладки «Саундборд»")):
        path = os.path.join(root, dirname)
        exists = os.path.isdir(path)
        line(OK if exists else INFO, f"каталог {dirname}/",
             f"{purpose}: существует" if exists else f"{purpose}: будет создан при запуске")
    # права на запись — именно из-за них фото/снимки «не сохранялись»
    for dirname in ("known_faces", "debug_frames", "voice_cache", "admin_data"):
        check_writable(root, dirname)

    # --- пакет facegate ---
    sys.path.insert(0, root)
    try:
        import facegate  # noqa: PLC0415
        line(OK, "пакет facegate", f"v{facegate.VERSION}")
    except Exception as e:  # noqa: BLE001
        line(FAIL, "пакет facegate", f"{type(e).__name__}: {e}",
             "запускайте из корня проекта: python server.py")

    # --- итог ---
    print("=" * 78)
    return finish()


def finish():
    if _problems:
        print(f"РЕЗУЛЬТАТ: FAIL — критичных проблем: {len(_problems)}")
        for p in _problems:
            print(f"  • {p}")
        print()
        print("Установка одной командой:  ./install.sh   (Windows: install.bat)")
        print("Ручная установка:          python -m pip install --force-reinstall "
              "\"setuptools<70\"")
        print("                           python -m pip install -r requirements.txt")
        return 1
    if _warnings:
        print(f"РЕЗУЛЬТАТ: OK с предупреждениями ({len(_warnings)})")
        for w in _warnings:
            print(f"  • {w}")
        return 0
    print("РЕЗУЛЬТАТ: OK — все зависимости на месте")
    return 0


if __name__ == "__main__":
    sys.exit(main())
