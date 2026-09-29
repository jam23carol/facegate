#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Клиент: захват видео с камеры, отправка на сервер, воспроизведение приветствий.

Клиент **полностью автономный** (не импортирует пакет facegate) — его можно
скопировать одним файлом на любую машину с камерой: Raspberry Pi, ПК с веб-камерой
и т.д. Нужны только ``opencv``, ``pyzmq`` и ``numpy``.

Окно с камерой на клиенте остаётся (это машина с монитором), а сервер никаких
окон не открывает — видео целиком видно в админ-панели:
``http://<адрес сервера>:8080``

Кадр уходит в ZeroMQ PUSH как multipart:
``[client_id, jpeg]``              — обычный кадр
``[client_id, jpeg, meta_json]``   — раз в --meta-interval секунд добавляются
метаданные (хост, камера, разрешение, версия),
они показываются в панели на карточке камеры.

Подключение к серверу — аргументами или переменными окружения (удобно в Docker):
--server-ip    / SERVER_HOST           (по умолчанию 127.0.0.1)
--video-port   / SERVER_VIDEO_PORT     (по умолчанию 5555)
--command-port / SERVER_COMMAND_PORT   (по умолчанию 5556)
--camera-id    / CAMERA_ID             (по умолчанию auto — первая работающая)
--camera-device / CAMERA_DEVICE        (приоритетнее --camera-id; удобно в Docker)
--client-id    / CLIENT_ID             (по умолчанию hostname)

Камера определяется автоматически: клиент перебирает /dev/video* (многие камеры
создают несколько узлов, где video0 — не всегда захват), затем индексы 0..9 —
и берёт первое устройство, которое реально читает кадры.

**Камера в Docker на хосте с Windows**
---------------------------------------
В Docker Desktop (WSL2 backend) USB-камеры не пробрасываются в контейнер
автоматически. Чтобы камера появилась как /dev/video* внутри контейнера:

1. Установите ``usbipd-win`` на хосте (один раз)::

       winget install usbipd

2. Найдите камеру в списке::

       usbipd list

3. Привяжите камеру к WSL2::

       usbipd attach --wsl --busid <busid-камеры>

4. Проверьте, что камера видна в контейнере::

       docker compose --profile client exec client ls /dev/video*

5. Перезапустите клиент::

       docker compose --profile client up -d client

Альтернатива — запуск клиента **нативно** на хосте с Windows (вне Docker)::

    run-client.bat --server-ip <IP-сервера>

В этом случае камера доступна напрямую через DirectShow/MSMF без проброса.

Примеры:
    python client.py --server-ip 192.168.1.10            # сторонний сервер в сети
    SERVER_HOST=face.example.com python client.py        # то же через env
    python client.py --list-cameras                      # какие камеры доступны
    python client.py --camera-id /dev/video2             # конкретное устройство
    CAMERA_DEVICE=/dev/video0 python client.py           # то же через env (Docker)
    python client.py --no-audio                          # без озвучки
"""
import argparse
import base64
import glob
import hashlib
import io
import json
import logging
import os
import queue
import re
import shutil
import signal
import socket as sock
import subprocess
import sys
import tempfile
import threading
import time
import wave

import cv2
import zmq

try:  # numpy идёт вместе с opencv; нужен для корректного ресемплинга
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

CLIENT_VERSION = "2.3.1"

# ---------- АУДИО: параметры ----------
#: Целевая частота дискретизации. 48 кГц аппаратно поддерживается почти всеми
#: звуковыми картами (в отличие от 24 кГц, которые отдаёт Silero TTS).
TARGET_SAMPLE_RATE = int(os.environ.get("AUDIO_TARGET_RATE", "48000") or 48000)
#: Тишина в конце фразы, мс — защита от обрезки последнего слога плеером.
TRAILING_PAD_MS = int(os.environ.get("AUDIO_PAD_MS", "150") or 150)
LEADING_PAD_MS = 15
#: Если пик громкости ниже порога — плавно поднимаем (тихая озвучка «не слышна»).
QUIET_PEAK = 0.06
QUIET_TARGET = 0.75
MAX_BOOST = 8.0
#: Сколько секунд хранить отпечаток проигранного звука (дедупликация).
DEDUPE_WINDOW = 2.0
#: Ограничение очереди: длинная очередь озвучки бессмысленна (всё устарело).
MAX_QUEUE = 6
#: Запас времени на воспроизведение файла внешним плеером.
PLAYER_EXTRA_SECONDS = 20.0

# Платформенные списки внешних плееров. Элементы, начинающиеся с "@", —
# встроенные способы воспроизведения (не ищутся в PATH):
#   @winsound    — Windows: winsound.PlaySound (встроен в Python)
#   @powershell  — Windows: Media.SoundPlayer через powershell
LINUX_PLAYERS = [
    ["paplay"],
    ["pw-play"],
    ["aplay", "-q"],
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    ["mpv", "--no-video", "--really-quiet"],
    ["play", "-q"],
    ["mplayer", "-really-quiet"],
    ["vlc", "--intf", "dummy", "--play-and-exit", "--no-video", "--quiet"],
]
WINDOWS_PLAYERS = [
    ["@powershell"],
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    ["mpv", "--no-video", "--really-quiet"],
    ["vlc", "--intf", "dummy", "--play-and-exit", "--no-video", "--quiet"],
]
MACOS_PLAYERS = [
    ["afplay"],
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    ["mpv", "--no-video", "--really-quiet"],
]


def platform_players():
    """Список кандидатов-плееров для текущей ОС (по порядку предпочтения)."""
    if sys.platform == "win32":
        return [list(p) for p in WINDOWS_PLAYERS]
    if sys.platform == "darwin":
        return [list(p) for p in MACOS_PLAYERS]
    return [list(p) for p in LINUX_PLAYERS]


# Сохраняем прежнее имя константы (используется в тестах и диагностике)
DEFAULT_PLAYERS = platform_players()


def _env(name, default=None):
    """Переменная окружения (пустая строка считается «не задано»)."""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name, default):
    try:
        return int(_env(name, default))
    except (TypeError, ValueError):
        log.warning("%s=%r — не целое число, использую %s", name, os.environ.get(name), default)
        return default


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Клиент отправки видео на сервер распознавания лиц "
                    "(поддерживает конфигурацию через переменные окружения)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--server-ip", type=str,
                        default=_env("SERVER_HOST", "127.0.0.1"),
                        help="IP/hostname сервера (env: SERVER_HOST). В одной docker-сети "
                             "это имя сервиса — server; для стороннего сервера — его адрес")
    parser.add_argument("--video-port", type=int,
                        default=_env_int("SERVER_VIDEO_PORT", 5555),
                        help="Порт для отправки видео (PUSH) (env: SERVER_VIDEO_PORT)")
    parser.add_argument("--command-port", type=int,
                        default=_env_int("SERVER_COMMAND_PORT", 5556),
                        help="Порт для получения команд (SUB) (env: SERVER_COMMAND_PORT)")
    parser.add_argument("--camera-id", type=str,
                        default=_env("CAMERA_ID", "auto"),
                        help="Камера: 'auto' — найти первую работающую автоматически "
                             "(/dev/video*, затем индексы 0..9), либо индекс (0, 1, ...) "
                             "или путь устройства (/dev/video2) (env: CAMERA_ID)")
    parser.add_argument("--camera-device", type=str,
                        default=_env("CAMERA_DEVICE"),
                        help="Путь к устройству камеры (приоритетнее --camera-id). "
                             "Удобно в Docker: CAMERA_DEVICE=/dev/video0. "
                             "Если задано — автопоиск не выполняется (env: CAMERA_DEVICE)")
    parser.add_argument("--video-source", type=str, default=_env("VIDEO_SOURCE"),
                        help="Источник видео вместо камеры: путь к файлу или RTSP/HTTP URL")
    parser.add_argument("--fps", type=int, default=10, help="Кадров в секунду для отправки")
    parser.add_argument("--client-id", type=str,
                        default=_env("CLIENT_ID") or sock.gethostname(),
                        help="Идентификатор клиента (адресат команд) (env: CLIENT_ID)")
    parser.add_argument("--jpeg-quality", type=int, default=80,
                        help="Качество JPEG отправляемых кадров (25-95)")
    parser.add_argument("--max-width", type=int, default=0,
                        help="Уменьшать кадр до этой ширины перед отправкой (0 = как есть)")
    parser.add_argument("--meta-interval", type=float, default=2.0,
                        help="Как часто добавлять к кадру метаданные, сек")
    parser.add_argument("--no-debug", action="store_true",
                        help="Не показывать окно с камерой (headless)")
    parser.add_argument("--no-audio", action="store_true", help="Не проигрывать приветствия вслух")
    parser.add_argument("--audio-player", type=str, default=_env("AUDIO_PLAYER"),
                        help="Команда проигрывания WAV (по умолчанию авто: "
                             "Windows — winsound, Linux — paplay/aplay/ffplay, macOS — afplay)")
    parser.add_argument("--audio-rate", type=int, default=_env_int("AUDIO_TARGET_RATE",
                                                                   TARGET_SAMPLE_RATE),
                        help="Частота дискретизации для воспроизведения, Гц "
                             "(48000 или 44100 — то, что поддерживает звуковая карта)")
    parser.add_argument("--audio-interrupt", action="store_true",
                        help="Прерывать текущий звук при новой команде "
                             "(по умолчанию звуки играются очередью, без обрывов)")
    parser.add_argument("--audio-dedupe", type=float, default=DEDUPE_WINDOW,
                        help="Окно дедупликации одинаковых звуков, сек (0 = выключить)")
    parser.add_argument("--audio-quiet", action="store_true",
                        help="Не писать в лог сообщения о воспроизведении")
    parser.add_argument("--list-cameras", action="store_true",
                        help="Проверить доступные камеры (0..9) и уйти")
    parser.add_argument("--loop-video", action="store_true",
                        help="Зациклить видеофайл (--video-source) вместо остановки")
    parser.add_argument("--check-audio", action="store_true",
                        help="Проверить звуковой тракт (тестовый сигнал) и уйти")
    return parser.parse_args(argv)


# ---------- АУДИО: доступность выходов ----------

def pulse_socket_path():
    """Путь к сокету PulseAudio/PipeWire (если задан/угадывается)."""
    srv = os.environ.get("PULSE_SERVER", "")
    if srv.startswith("unix:"):
        return srv[5:].split(";")[0]
    xrd = os.environ.get("XDG_RUNTIME_DIR")
    if xrd:
        return os.path.join(xrd, "pulse", "native")
    try:
        return f"/run/user/{os.getuid()}/pulse/native"
    except AttributeError:  # Windows
        return None


def pulse_available():
    """PulseAudio-сокет реально существует (иначе paplay молча не сыграет)."""
    path = pulse_socket_path()
    return bool(path) and os.path.exists(path)


def alsa_available():
    """Есть ли ALSA-устройства (/dev/snd) — прямой путь звука без PulseAudio."""
    return os.path.isdir("/dev/snd")


def native_player():
    """Встроенный (не из PATH) способ воспроизвести WAV, либо None."""
    if sys.platform != "win32":
        return None
    try:
        import winsound  # noqa: F401
        return ["@winsound"]
    except ImportError:  # pragma: no cover - Windows без winsound не бывает
        return ["@powershell"]


def _usable(candidate):
    """Плеер реально доступен: встроенный маркер («@…») или бинарник в PATH."""
    if not candidate:
        return False
    if str(candidate[0]).startswith("@"):
        return True
    return bool(shutil.which(candidate[0]))


def player_candidates(player_cmd=None):
    """Все **доступные** плееры по порядку предпочтения (для перебора при сбоях)."""
    ordered = []

    def add(candidate):
        if candidate and candidate not in ordered and _usable(candidate):
            ordered.append(list(candidate))

    if player_cmd:
        parts = player_cmd.split() if isinstance(player_cmd, str) else list(player_cmd)
        if parts and _usable(parts):
            add(parts)
        elif parts:
            log.warning("Указанный плеер не найден: %s", player_cmd)
    add(native_player())
    if pulse_available():
        add(["paplay"])
    if alsa_available():
        add(["aplay", "-q"])
    for candidate in platform_players():
        add(candidate)
    return ordered


def resolve_player(player_cmd=None):
    """Возвращает список аргументов первого подходящего плеера либо None."""
    candidates = player_candidates(player_cmd)
    return candidates[0] if candidates else None


def player_hint():
    """Подсказка для лога: что поставить, если плеер не найден."""
    if sys.platform == "win32":
        return ("Установите любой из: Media Player (обычно уже есть), "
                "mpv/ffmpeg (ffplay) или VLC и добавьте их в PATH; "
                "либо укажите --audio-player \"путь\\к\\плееру\"")
    if sys.platform == "darwin":
        return "Ожидается afplay (входит в macOS) либо ffplay/mpv из Homebrew"
    return ("Установите pulseaudio-utils (paplay) или alsa-utils (aplay): "
            "sudo apt install pulseaudio-utils alsa-utils; "
            "либо ffplay/mpv: sudo apt install ffmpeg mpv")


# ---------- АУДИО: нормализация WAV ----------

def _wav_to_float(raw, params):
    """PCM-байты WAV → float32-массив в диапазоне [-1, 1] (моно)."""
    if np is None:  # pragma: no cover - numpy идёт вместе с opencv
        return None, params.framerate
    width, channels = params.sampwidth, max(1, params.nchannels)
    if width == 1:
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        data = (data - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:  # 24 бит
        count = len(raw) // 3
        buf = np.frombuffer(raw[:count * 3], dtype=np.uint8).astype(np.int32)
        buf = buf.reshape(-1, 3)
        data = ((buf[:, 0] | (buf[:, 1] << 8) | (buf[:, 2] << 16))).astype(np.int32)
        data[data >= 1 << 23] -= 1 << 24
        data = data.astype(np.float32) / float(1 << 23)
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
    else:
        return None, params.framerate
    if channels > 1 and data.size >= channels:
        usable = (data.size // channels) * channels
        data = data[:usable].reshape(-1, channels).mean(axis=1)
    return data.astype(np.float32), params.framerate


def _resample(samples, src_rate, dst_rate):
    """Линейный ресемплинг моно-потока (чистая numpy-реализация, scipy не нужен)."""
    if src_rate == dst_rate or samples is None or samples.size == 0:
        return samples
    duration = samples.size / float(src_rate)
    count = int(round(duration * dst_rate))
    if count <= 0:
        return samples
    positions = np.linspace(0.0, samples.size - 1, num=count, dtype=np.float64)
    return np.interp(positions, np.arange(samples.size, dtype=np.float64),
                     samples).astype(np.float32)


def normalize_wav(data, target_rate=TARGET_SAMPLE_RATE, pad_ms=TRAILING_PAD_MS,
                  quiet_boost=True):
    """Приводит WAV к формату, который реально играет системный плеер."""
    info = {"rate": None, "seconds": 0.0, "converted": False, "error": None}
    if not data:
        info["error"] = "пустые аудио-данные"
        return data, info
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            params = w.getparams()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError, ValueError) as e:
        info["error"] = f"не удалось разобрать WAV ({e})"
        return data, info
    if np is None:  # pragma: no cover
        info["error"] = "numpy недоступен — WAV отправлен без нормализации"
        return data, info
    samples, src_rate = _wav_to_float(raw, params)
    if samples is None or samples.size == 0:
        info["error"] = "в WAV нет аудиоданных"
        return data, info
    info["rate"] = int(src_rate)
    dst_rate = int(target_rate or TARGET_SAMPLE_RATE)
    samples = _resample(samples, src_rate, dst_rate)
    if src_rate != dst_rate:
        info["converted"] = True
    if quiet_boost:
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        if 0 < peak < QUIET_PEAK:
            gain = min(QUIET_TARGET / peak, MAX_BOOST)
            samples = np.clip(samples * gain, -1.0, 1.0).astype(np.float32)
            info["boosted"] = round(gain, 2)
    lead = np.zeros(int(dst_rate * LEADING_PAD_MS / 1000.0), dtype=np.float32)
    tail = np.zeros(int(dst_rate * max(0, pad_ms) / 1000.0), dtype=np.float32)
    samples = np.concatenate([lead, samples.astype(np.float32), tail])
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(dst_rate)
        w.writeframes(pcm.tobytes())
    info["seconds"] = round(pcm.size / float(dst_rate), 3)
    info["out_rate"] = dst_rate
    return buf.getvalue(), info


def test_tone_wav(seconds=0.6, rate=TARGET_SAMPLE_RATE, freq=440.0):
    """Синусоидальный тестовый сигнал (для --check-audio)."""
    if np is None:  # pragma: no cover
        return None
    t = np.linspace(0.0, seconds, int(seconds * rate), endpoint=False)
    pcm = (0.5 * np.sin(2 * np.pi * freq * t) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# ---------- АУДИО: очередь воспроизведения ----------

class PlaybackItem:
    __slots__ = ("data", "started", "finished", "result", "message", "digest",
                 "created")

    def __init__(self, data):
        self.data = data
        self.started = threading.Event()    # воспроизведение началось
        self.finished = threading.Event()   # воспроизведение завершилось
        self.result = False
        self.message = ""
        self.digest = hashlib.sha1(data or b"").hexdigest()[:16]
        self.created = time.time()


class AudioPlayer:
    """Последовательное воспроизведение в одном потоке."""

    def __init__(self, player_cmd=None, target_rate=TARGET_SAMPLE_RATE,
                 interrupt=False, dedupe=DEDUPE_WINDOW, max_queue=MAX_QUEUE,
                 quiet=False):
        self.player_cmd = player_cmd
        self.target_rate = int(target_rate or TARGET_SAMPLE_RATE)
        self.interrupt = bool(interrupt)
        self.dedupe = float(dedupe or 0.0)
        self.max_queue = int(max_queue)
        self.quiet = bool(quiet)
        self._queue = queue.Queue(maxsize=self.max_queue)
        self._played = {}
        self._lock = threading.Lock()
        self._thread = None
        self._current = None
        self._proc = None
        self.stats = {"played": 0, "failed": 0, "skipped": 0, "dropped": 0}

    # ----- публичное -----

    def start(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True,
                                                name="audio-player")
                self._thread.start()
        return self

    def submit(self, data, wait=3.0):
        """Ставит звук в очередь. Возвращает True, если воспроизведение началось."""
        if not data:
            return False
        self.start()
        item = PlaybackItem(data)
        if self._is_duplicate(item):
            self.stats["skipped"] += 1
            self._log("Повтор того же звука в течение %.1f с — пропущен", self.dedupe)
            return True
        if self.interrupt:
            self.stop_all()
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.stats["dropped"] += 1
            log.warning("Очередь воспроизведения переполнена (%d) — звук отброшен",
                        self.max_queue)
            return False
        item.started.wait(timeout=max(0.1, float(wait)))
        if item.finished.is_set():
            return bool(item.result)
        return True

    def stop_all(self):
        """Очищает очередь и прерывает текущее воспроизведение (команда ``stop``)."""
        with self._lock:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            proc = self._proc
            if proc is not None:
                self._kill(proc)

    def describe(self):
        player = resolve_player(self.player_cmd)
        return {
            "player": " ".join(player) if player else None,
            "target_rate": self.target_rate,
            "queue": self._queue.qsize(),
            "stats": dict(self.stats),
        }

    # ----- внутреннее -----

    def _log(self, message, *args):
        if not self.quiet:
            log.info(message, *args)

    def _is_duplicate(self, item):
        if self.dedupe <= 0:
            return False
        now = time.time()
        with self._lock:
            for key in list(self._played):
                if now - self._played[key] > max(self.dedupe, 10.0):
                    self._played.pop(key, None)
            last = self._played.get(item.digest)
            self._played[item.digest] = now
            return bool(last and (now - last) <= self.dedupe)

    def _loop(self):
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._current = item
            item.started.set()
            try:
                ok, message = self._play_blocking(item)
            except Exception as e:  # noqa: BLE001 - плеер не должен ронять клиент
                ok, message = False, f"{type(e).__name__}: {e}"
            item.result = ok
            item.message = message
            self.stats["played" if ok else "failed"] += 1
            self._current = None
            item.finished.set()

    def _play_blocking(self, item):
        data, info = normalize_wav(item.data, target_rate=self.target_rate)
        if info.get("error"):
            log.warning("Аудио: %s", info["error"])
        if info.get("converted"):
            self._log("Аудио приведено %s Гц → %d Гц (%.2f с)",
                      info.get("rate"), info.get("out_rate", self.target_rate),
                      info.get("seconds", 0.0))
        path = self._write_temp(data)
        if path is None:
            return False, "не удалось записать временный WAV"
        try:
            return self._try_players(path, info)
        finally:
            self._cleanup(path, info.get("seconds") or 0.0)

    @staticmethod
    def _write_temp(data):
        try:
            fd, path = tempfile.mkstemp(suffix=".wav", prefix="greet_")
        except OSError as e:
            log.error("Не удалось создать временный файл: %s", e)
            return None
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except OSError as e:
            log.error("Не удалось записать временный WAV: %s", e)
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        return path

    def _try_players(self, path, info):
        timeout = float(info.get("seconds") or 5.0) + PLAYER_EXTRA_SECONDS
        errors = []
        for candidate in player_candidates(self.player_cmd):
            name = candidate[0]
            ok, message = self._play_one(candidate, path, timeout)
            if ok:
                self._log("Проиграно через %s (%.2f с)", name, info.get("seconds") or 0.0)
                return True, name
            errors.append(f"{name}: {message}")
            log.warning("Плеер %s не смог воспроизвести звук: %s", name, message or "ошибка")
        log.error("Звук не воспроизведён. %s", player_hint())
        for line in errors[-3:]:
            log.error("  · %s", line)
        return False, "; ".join(errors)

    def _play_one(self, candidate, path, timeout):
        name = candidate[0]
        if name == "@winsound":
            return self._play_winsound(path)
        if name == "@powershell":
            return self._run_external_player(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(New-Object Media.SoundPlayer '{path}').PlaySync()"], path, timeout)
        if not shutil.which(name):
            return False, "не найден в PATH"
        cmd = list(candidate)
        if name == "aplay":
            cmd = ["aplay", "-q", "-f", "S16_LE", "-r", str(self.target_rate),
                   "-c", "1", "-t", "wav"]
        return self._run_external_player(cmd, path, timeout)

    @staticmethod
    def _play_winsound(path):
        try:
            import winsound
        except ImportError:  # pragma: no cover
            return False, "winsound недоступен"
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME)
            return True, ""
        except RuntimeError as e:
            return False, str(e)

    def _run_external_player(self, cmd, path, timeout):
        """Запускает внешний плеер и ЖДЁТ завершения."""
        full = cmd + [path]
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(full, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, **kwargs)
        except OSError as e:
            return False, str(e)
        with self._lock:
            self._proc = proc
        try:
            _out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            return False, f"таймаут {timeout:.0f} с"
        except Exception as e:  # noqa: BLE001 - плеер мог быть убит из stop_all()
            self._kill(proc)
            return False, str(e)
        finally:
            with self._lock:
                self._proc = None
        if proc.returncode != 0:
            detail = (err or "").strip().replace("\n", " ")
            return False, detail[-300:] or f"код возврата {proc.returncode}"
        return True, ""

    @staticmethod
    def _kill(proc):
        try:
            proc.kill()
        except OSError:  # pragma: no cover
            pass
        try:
            proc.communicate(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _cleanup(path, seconds):
        delay = max(2.0, float(seconds) + 2.0)

        def _remove():
            try:
                os.remove(path)
            except OSError:
                pass

        timer = threading.Timer(delay, _remove)
        timer.daemon = True
        timer.start()


# ---------- АУДИО: глобальный плеер и прежний API ----------

_PLAYER = None
_PLAYER_LOCK = threading.Lock()


def audio_player(player_cmd=None, target_rate=TARGET_SAMPLE_RATE, interrupt=False,
                 dedupe=DEDUPE_WINDOW, quiet=False):
    """Единственный экземпляр :class:`AudioPlayer` на процесс (с очередью)."""
    global _PLAYER
    with _PLAYER_LOCK:
        if _PLAYER is None:
            _PLAYER = AudioPlayer(player_cmd=player_cmd, target_rate=target_rate,
                                  interrupt=interrupt, dedupe=dedupe, quiet=quiet)
        else:
            _PLAYER.player_cmd = player_cmd or _PLAYER.player_cmd
            _PLAYER.interrupt = interrupt
    return _PLAYER.start()


def stop_audio():
    """Останавливает очередь воспроизведения (команда ``stop`` от сервера)."""
    with _PLAYER_LOCK:
        if _PLAYER is not None:
            _PLAYER.stop_all()


def play_wav_bytes(data, player_cmd=None, wait=3.0, **kwargs):
    """Воспроизведение WAV из памяти. Возвращает True, если звук запущен."""
    if not data:
        return False
    if not player_candidates(player_cmd):
        log.error("Не найден аудиоплеер. %s", player_hint())
        return False
    player = audio_player(player_cmd,
                          target_rate=kwargs.get("target_rate", TARGET_SAMPLE_RATE),
                          interrupt=kwargs.get("interrupt", False),
                          dedupe=kwargs.get("dedupe", DEDUPE_WINDOW),
                          quiet=kwargs.get("quiet", False))
    return player.submit(data, wait=wait)


def _cleanup_later(path, delay=30.0):
    """Устаревший помощник (оставлен для совместимости со старыми вызовами)."""
    def _rm():
        try:
            os.remove(path)
        except OSError:
            pass

    t = threading.Timer(delay, _rm)
    t.daemon = True
    t.start()


def decode_audio_b64(audio_b64):
    try:
        return base64.b64decode(audio_b64)
    except Exception as e:  # noqa: BLE001 - битый base64 не должен ронять клиент
        log.error("Не удалось декодировать аудио: %s", e)
        return None


# ---------- КОМАНДЫ ----------

def handle_command(msg, play=True, player_cmd=None):
    """Обрабатывает одну команду от сервера. Возвращает описание действия."""
    action = msg.get("action")
    audio_b64 = msg.get("audio_b64")
    played = False
    if action in ("greet", "speak"):
        if action == "greet":
            text = msg.get("text") or f"Здравствуйте, {msg.get('name', 'гость')}!"
            log.info("Приветствие: %s", text)
        else:
            text = msg.get("text") or ""
            log.info("📢 Объявление%s: %s",
                     f" от {msg.get('source')}" if msg.get("source") else "", text)
        if audio_b64 and play:
            data = decode_audio_b64(audio_b64)
            if data:
                try:
                    played = play_wav_bytes(data, player_cmd)
                except Exception as e:  # noqa: BLE001
                    log.error("Ошибка воспроизведения: %s", e)
            else:
                log.error("Команда содержит некорректное аудио (base64)")
        elif audio_b64 and not play:
            log.info("Воспроизведение отключено (--no-audio)")
        elif not audio_b64:
            log.info("Команда без аудио — озвучка на сервере недоступна/ещё готовится")
        return {"action": action, "name": msg.get("name"), "text": text, "played": played}
    if action == "stop":
        stop_audio()
        log.info("Воспроизведение остановлено по команде сервера")
        return {"action": action, "stopped": True}
    if action == "ping":
        log.info("Пинг от сервера: %s", msg.get("text") or "")
        return {"action": action, "pong": True}
    log.info("Получена команда: %s", msg)
    return {"action": action}


# ---------- ПОТОК ДЛЯ ПРИЁМА КОМАНД ----------

def command_listener(args, stop_event=None):
    context = zmq.Context()
    sub_socket = context.socket(zmq.SUB)
    sub_socket.setsockopt(zmq.LINGER, 0)
    sub_socket.connect(f"tcp://{args.server_ip}:{args.command_port}")
    sub_socket.setsockopt_string(zmq.SUBSCRIBE, args.client_id)
    sub_socket.setsockopt(zmq.RCVTIMEO, 300)
    log.info("Подписан на команды для %s", args.client_id)
    while not (stop_event and stop_event.is_set()):
        try:
            _topic = sub_socket.recv_string()
            payload = sub_socket.recv_string()
            msg = json.loads(payload)
            handle_command(msg, play=not args.no_audio, player_cmd=args.audio_player)
        except zmq.Again:
            continue
        except Exception as e:  # noqa: BLE001 - слушающий поток не должен умирать
            if stop_event and stop_event.is_set():
                break
            log.error("Ошибка в listener: %s", e)
            time.sleep(0.1)
    try:
        sub_socket.close(0)
        context.term()
    except Exception:  # noqa: BLE001
        pass


# ---------- ВИДЕО ----------

def prepare_frame(frame, max_width=0):
    """Уменьшает кадр, если задано --max-width. Возвращает (кадр, (w, h))."""
    h, w = frame.shape[:2]
    if max_width and w > max_width:
        scale = max_width / float(w)
        frame = cv2.resize(frame, (max_width, max(1, int(round(h * scale)))),
                           interpolation=cv2.INTER_AREA)
        h, w = frame.shape[:2]
    return frame, (w, h)


def encode_jpeg(frame, quality=80):
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else None


def save_jpeg(path, frame, quality=90):
    """Сохраняет кадр кроссплатформенно."""
    data = encode_jpeg(frame, quality)
    if not data:
        log.error("Не удалось закодировать кадр для сохранения")
        return False
    try:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        log.error("Не удалось сохранить кадр %s: %s", path, e)
        return False
    if not os.path.exists(path) or os.path.getsize(path) <= 0:
        log.error("Файл %s не создан (проверьте права на каталог)", path)
        return False
    return True


def build_meta(args, width, height, sent_frames=0, fps_actual=0.0):
    """Метаданные клиента для админ-панели сервера."""
    return {
        "client_id": args.client_id,
        "hostname": sock.gethostname(),
        "camera_id": args.camera_id if not args.video_source else None,
        "source": args.video_source or f"camera:{args.camera_id}",
        "fps": args.fps,
        "fps_actual": round(fps_actual, 1),
        "width": width,
        "height": height,
        "jpeg_quality": args.jpeg_quality,
        "sent_frames": sent_frames,
        "version": CLIENT_VERSION,
        "ts": time.time(),
    }


def _is_capture_device(video_name):
    """True, если /sys говорит, что это устройство захвата, а не метаданных."""
    sys_path = f"/sys/class/video4linux/{video_name}/device"
    return os.path.isdir(sys_path)


def _device_caps(video_name):
    """Прочитать capabilities V4L2-устройства из /sys (если возможно)."""
    caps_path = f"/sys/class/video4linux/{video_name}/device/capabilities"
    try:
        with open(caps_path, "r") as f:
            return f.read().strip()
    except (OSError, ValueError):
        return ""


def video_devices():
    """Список /dev/video* (Linux), отсортированный по номеру устройства.

    Предпочтение отдаётся устройствам, которые являются реальными захватчиками
    (имеют /sys/class/video4linux/videoN/device/), а не узлами метаданных.
    Если фильтрация через /sys невозможна — возвращаются все найденные.
    """
    def num(path):
        m = re.search(r"(\d+)$", path)
        return int(m.group(1)) if m else 0

    if sys.platform == "win32":
        return []

    devices = sorted(glob.glob("/dev/video*"), key=num)
    if not devices:
        return []

    # Разделяем на реальные устройства захвата и метаданные
    capture, metadata = [], []
    for dev in devices:
        name = os.path.basename(dev)
        if _is_capture_device(name):
            capture.append(dev)
        else:
            metadata.append(dev)

    # Если удалось найти реальные устройства захвата — идём по ним первыми,
    # но метаданные тоже пробуем (на случай, если /sys неполный)
    ordered = capture + metadata
    return ordered if ordered else devices


def windows_camera_devices(max_index=10):
    """Индексы камер для Windows (DirectShow/MSMF)."""
    return list(range(max_index))


def try_open_capture(source):
    """Пробует открыть источник и прочитать кадр. Возвращает cap или None."""
    cap = cv2.VideoCapture(source)
    try:
        if cap.isOpened():
            ret, frame = cap.read()
            if ret and frame is not None:
                return cap
    except Exception as e:  # noqa: BLE001 - драйверы камер бывают разные
        log.debug("Источник %s открылся, но кадр не читается: %s", source, e)
    try:
        cap.release()
    except Exception:  # noqa: BLE001
        pass
    return None


def autodetect_camera(max_index=10):
    """Автопоиск камеры: сначала /dev/video*, затем индексы 0..max_index.

    Многие веб-камеры создают несколько узлов (/dev/video0 — захват,
    /dev/video1 — метаданные и т.д.), поэтому «камера 0» не всегда рабочая.

    Возвращает (источник, открытый VideoCapture) или (None, None).
    """
    candidates = list(video_devices())
    candidates += [i for i in range(max_index)
                   if f"/dev/video{i}" not in candidates]

    for source in candidates:
        cap = try_open_capture(source)
        if cap is not None:
            log.info("📷 Камера определена автоматически: %s", source)
            return source, cap
        log.info("Камера %s: недоступна или не читает кадры — пробую следующую", source)

    # --- Диагностика при неудаче ---
    if sys.platform != "win32":
        dev_video = sorted(glob.glob("/dev/video*"))
        if not dev_video:
            log.warning("В контейнере нет устройств /dev/video* — камера хоста "
                        "не проброшена.")
            if _in_docker():
                log.warning("Похоже, клиент работает в Docker на хосте с Windows.")
                log.warning("Для проброса камеры в WSL2/Docker на хосте с Windows:")
                log.warning("  1. Установите на хосте:  winget install usbipd")
                log.warning("  2. Найдите камеру:       usbipd list")
                log.warning("  3. Привяжите к WSL2:     usbipd attach --wsl --busid <busid>")
                log.warning("  4. Проверьте:            docker compose --profile client exec client ls /dev/video*")
                log.warning("  5. Перезапустите клиент: docker compose --profile client up -d client")
                log.warning("Альтернатива: запустите клиент нативно на хосте: run-client.bat")
        else:
            log.warning("Устройства %s найдены, но ни одно не читает кадры. "
                        "Проверьте права (группа video) и не занята ли камера "
                        "другим приложением.", ", ".join(dev_video))
    else:
        log.warning("Проверьте: Диспетчер устройств → камеры, права на камеру "
                    "в Windows (Параметры → Конфиденциальность → Камера), "
                    "не занята ли камера другим приложением")

    return None, None


def _in_docker():
    """Эвристика: клиент работает внутри контейнера."""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r") as f:
            return "docker" in f.read() or "kubepods" in f.read()
    except (OSError, ValueError):
        return False


def open_capture(args):
    """Открывает камеру (--camera-device / --camera-id auto|индекс|путь)
    или видеофайл/поток."""
    if args.video_source:
        cap = cv2.VideoCapture(args.video_source)
        if not cap.isOpened():
            log.error("Не удалось открыть источник видео: %s", args.video_source)
            return None
        args.resolved_source = args.video_source
        return cap

    # CAMERA_DEVICE имеет приоритет над CAMERA_ID
    camera_device = getattr(args, "camera_device", None)
    if camera_device:
        args.camera_id = camera_device

    cam = str(getattr(args, "camera_id", "auto") or "auto").strip()
    if cam.lower() in ("auto", "-1", ""):
        source, cap = autodetect_camera()
        if cap is None:
            log.error("Рабочая камера не найдена автоматически")
            if sys.platform == "win32":
                log.error("Проверьте: Диспетчер устройств → камеры, права на камеру "
                          "в Windows (Параметры → Конфиденциальность → Камера), "
                          "не занята ли камера другим приложением")
            else:
                log.error("Проверьте: ls -l /dev/video* | права (группа video) | "
                          "в Docker — device_cgroup_rules 'c 81:* rmw' и том /dev:/dev "
                          "(см. docker-compose.yml), либо задайте CAMERA_DEVICE явно")
            return None
        args.camera_id = source
        args.resolved_source = source
        return cap

    try:
        source = int(cam)
    except ValueError:
        source = cam          # путь устройства, например /dev/video2

    cap = try_open_capture(source)
    if cap is None:
        log.error("Не удалось открыть камеру: %s", source)
        log.error("Проверьте: существует ли устройство, права (группа video), "
                  "или запустите с CAMERA_ID=auto для автопоиска")
        if _in_docker() and not os.path.exists(str(source)):
            log.error("Устройство %s не существует в контейнере. "
                      "На хосте с Windows камера не проброшена — используйте "
                      "usbipd attach --wsl --busid <busid> или запустите клиент "
                      "нативно (run-client.bat).", source)
        return None
    args.camera_id = source
    args.resolved_source = source
    return cap


def list_cameras(count=10):
    """Перечисляет доступные камеры: /dev/video* и индексы 0..count."""
    found = []
    candidates = video_devices() + [i for i in range(count)]
    for source in candidates:
        cap = try_open_capture(source)
        if cap is not None:
            ret, frame = True, None
            try:
                ret, frame = cap.read()
            except Exception:  # noqa: BLE001
                ret = False
            if ret and frame is not None:
                h, w = frame.shape[:2]
                found.append((source, w, h))
                log.info("Камера %s: доступна (%dx%d)", source, w, h)
            cap.release()
        else:
            log.info("Камера %s: недоступна", source)
    if not found:
        log.warning("Ни одной рабочей камеры не найдено")
        if sys.platform != "win32" and _in_docker():
            log.warning("В Docker на хосте с Windows камеры не пробрасываются "
                        "автоматически. См. инструкцию в комментарии выше "
                        "или запустите клиент нативно: run-client.bat")
    return found


def check_audio(args):
    """Диагностика звукового тракта: плеер, тестовый сигнал, параметры."""
    print("=" * 72)
    print("Проверка звука")
    print("-" * 72)
    player = resolve_player(args.audio_player)
    print(f"  Платформа:            {sys.platform} ({os.name})")
    print(f"  Выбранный плеер:      {' '.join(player) if player else 'НЕ НАЙДЕН'}")
    print(f"  Все кандидаты:        "
          f"{', '.join(c[0] for c in player_candidates(args.audio_player))}")
    if sys.platform != "win32":
        print(f"  PulseAudio сокет:     {'есть' if pulse_available() else 'нет'}")
        print(f"  ALSA (/dev/snd):      {'есть' if alsa_available() else 'нет'}")
    print(f"  Целевая частота:      {args.audio_rate} Гц")
    if not player:
        print(f"\n✗ Плеер не найден. {player_hint()}")
        print("=" * 72)
        return 1
    tone = test_tone_wav(rate=int(args.audio_rate))
    print("\n▶ Играю тестовый сигнал 0.6 с (440 Гц)…")
    ok = play_wav_bytes(tone, args.audio_player, wait=10.0)
    time.sleep(1.2)
    print(f"  Результат:            {'успешно' if ok else 'ОШИБКА (см. лог выше)'}")
    info = audio_player().describe()
    print(f"  Статистика:           {info['stats']}")
    print("=" * 72)
    return 0 if ok else 1


def draw_hud(frame, client_id, fps_actual, server, show_status=True):
    """Служебная строка в окне клиента (cv2.putText не умеет кириллицу)."""
    if not show_status:
        return frame
    h = frame.shape[0]
    text = f"{client_id} -> {server} | {fps_actual:.1f} fps"
    cv2.rectangle(frame, (0, h - 24), (frame.shape[1], h), (24, 24, 24), -1)
    cv2.putText(frame, text, (8, h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (220, 220, 220), 1, cv2.LINE_AA)
    return frame


# ---------- ОСНОВНОЙ ПОТОК ОТПРАВКИ ВИДЕО ----------

def start_client(args):
    stop_event = threading.Event()

    def _sig(_signum, _frame):
        log.info("Получен сигнал остановки")
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except (ValueError, AttributeError):  # pragma: no cover
        pass

    if args.list_cameras:
        list_cameras()
        return 0
    if args.check_audio:
        return check_audio(args)

    # Звуковой тракт готовим сразу
    if not args.no_audio:
        player = resolve_player(args.audio_player)
        if player is None:
            log.error("Аудиоплеер не найден — озвучка работать не будет. %s", player_hint())
        else:
            log.info("Аудиоплеер: %s (частота %d Гц, очередь без обрывов)",
                     " ".join(player), args.audio_rate)
        audio_player(args.audio_player, target_rate=args.audio_rate,
                     interrupt=args.audio_interrupt, dedupe=args.audio_dedupe,
                     quiet=args.audio_quiet)

    threading.Thread(target=command_listener, args=(args, stop_event),
                     daemon=True, name="command-listener").start()

    context = zmq.Context()
    video_socket = context.socket(zmq.PUSH)
    video_socket.setsockopt(zmq.SNDHWM, 30)
    video_socket.setsockopt(zmq.LINGER, 0)
    video_socket.connect(f"tcp://{args.server_ip}:{args.video_port}")
    log.info("Подключен к серверу %s:%s", args.server_ip, args.video_port)

    cap = open_capture(args)
    if cap is None:
        return 1

    send_interval = 1.0 / max(args.fps, 1)
    show_debug = not args.no_debug
    if show_debug:
        log.info("Окно предпросмотра: «q» — выход, «s» — сохранить кадр. "
                 "На сервере видео видно в админ-панели.")
    log.info("Клиент запущен, отправка видео...")

    sent = 0
    last_meta = 0.0
    fps_actual = 0.0
    last_send = time.time()
    window_name = f"Client Camera — {args.client_id}"

    try:
        while not stop_event.is_set():
            ret, frame = cap.read()
            if not ret:
                if args.video_source and args.loop_video:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                log.warning("Источник видео исчерпан")
                break

            frame, (w, h) = prepare_frame(frame, args.max_width)

            if show_debug:
                try:
                    hud = draw_hud(frame.copy(), args.client_id, fps_actual,
                                   f"{args.server_ip}:{args.video_port}")
                    cv2.imshow(window_name, hud)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord("q"):
                        log.info("Выход по клавише q")
                        break
                    elif key == ord("s"):
                        path = os.path.join("client_frames", f"client_{sent:06d}.jpg")
                        if save_jpeg(path, frame):
                            log.info("Кадр сохранён: %s", path)
                except Exception as e:  # noqa: BLE001 - окно не критично
                    log.error("Ошибка окна: %s. Отключаю визуализацию.", e)
                    show_debug = False

            payload = encode_jpeg(frame, args.jpeg_quality)
            if payload:
                now = time.time()
                if now - last_meta >= max(args.meta_interval, 0.0):
                    last_meta = now
                    meta = build_meta(args, w, h, sent, fps_actual)
                    video_socket.send_string(args.client_id, flags=zmq.SNDMORE)
                    video_socket.send(payload, flags=zmq.SNDMORE)
                    video_socket.send_string(json.dumps(meta, ensure_ascii=False))
                else:
                    video_socket.send_string(args.client_id, flags=zmq.SNDMORE)
                    video_socket.send(payload)
                sent += 1

                dt = now - last_send
                last_send = now
                if dt > 0:
                    fps_actual = (0.9 * fps_actual + 0.1 * min(1.0 / dt, 200.0)) if fps_actual else min(1.0 / dt, 200.0)

            elapsed = time.time() - last_send
            if elapsed < send_interval:
                time.sleep(send_interval - elapsed)

    except KeyboardInterrupt:
        log.info("Клиент остановлен")
    finally:
        stop_event.set()
        cap.release()
        try:
            video_socket.close(0)
            context.term()
        except Exception:  # noqa: BLE001
            pass
        cv2.destroyAllWindows()

    stats = audio_player().describe()["stats"] if _PLAYER is not None else {}
    log.info("Отправлено кадров: %d%s", sent,
             f" | звук: {stats}" if stats else "")
    return 0


if __name__ == "__main__":
    sys.exit(start_client(parse_args()))
