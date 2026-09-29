# -*- coding: utf-8 -*-
"""Движки синтеза речи (TTS).

Задача — **нормальная русская озвучка**, а не «русский текст английским голосом»:

* :class:`SileroTTSEngine` — нейросетевой Silero TTS (официальный пакет
  ``silero``, русская модель ``v5_ru``; голоса ``aidar``, ``baya``, ``kseniya``,
  ``eugene``, ``xenia``). Это движок по умолчанию в Docker-образе сервера.
  Модель грузится **один раз** в отдельном процессе-воркере
  (``python -m facegate.tts.silero_worker``): сервер изолирован от torch,
  зависание синтеза не роняет сервер, а каждая следующая фраза
  синтезируется без повторной загрузки модели (десятые доли секунды).
* :class:`EspeakTTSEngine` — системный ``espeak-ng``/``espeak``. Роботизированный,
  но лёгкий запасной вариант; язык по умолчанию ``ru``, чтобы кириллица не
  читалась английским голосом.
* :class:`Pyttsx3TTSEngine` — ``pyttsx3`` (SAPI5 в Windows, NSSpeech в macOS,
  тот же espeak в Linux). Оставлен для запуска вне Docker на Windows/macOS.

Единый контракт: ``synthesize(text, out_path, voice, rate, speed, timeout)``
пишет WAV-файл и бросает :class:`TTSError` при неудаче.
"""
import importlib.util
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import wave

from facegate.tts.textnorm import looks_truncated, normalize_for_tts

log = logging.getLogger(__name__)

# Голоса Silero TTS v5 (модель v5_ru, официальный пакет `silero`)
SILERO_SPEAKERS = ("aidar", "baya", "kseniya", "eugene", "xenia")
SILERO_DEFAULT_SPEAKER = "baya"

# Частота дискретизации синтезированного WAV.
# 48 кГц выбрано потому, что это аппаратная частота подавляющего большинства
# звуковых карт: aplay/DirectSound играют такой файл без передискретизации.
# Раньше здесь было 24000 Гц — ALSA на многих чипах отказывалась играть такой
# поток («Rate 24000Hz not supported») либо воспроизводила его вдвое быстрее
# («голос бурундука», фраза в два раза короче). Silero v5 поддерживает 48 кГц;
# если модель откажется, воркер сам откатится на 24 кГц, а клиент всё равно
# приводит WAV к 48 кГц перед воспроизведением (см. client.py:normalize_wav).
SILERO_SAMPLE_RATE = int(os.environ.get("SILERO_SAMPLE_RATE", "48000") or 48000)
SILERO_FALLBACK_SAMPLE_RATE = 24000

# Тишина в конце фразы, мс — плееры (ffplay/aplay) иначе «съедают» последний слог
SILERO_PAD_MS = int(os.environ.get("SILERO_PAD_MS", "150") or 150)


class TTSError(RuntimeError):
    """Ошибка синтеза речи (движок не найден, процесс упал, таймаут…)."""


# ---------------------------------------------------------------------------
# Базовый контракт
# ---------------------------------------------------------------------------
class BaseTTSEngine:
    name = "base"

    def available(self):
        """Доступен ли движок в текущем окружении (без запуска воркеров)."""
        return False

    def synthesize(self, text, out_path, voice=None, rate=None, speed=None,
                   timeout=60.0):
        """Синтезирует ``text`` в WAV-файл ``out_path``. Бросает TTSError."""
        raise TTSError(f"Движок {self.name} не реализован")

    def close(self):
        """Освобождает ресурсы (процессы, модели)."""


# ---------------------------------------------------------------------------
# Silero TTS (нейросетевой, качественный русский голос)
# ---------------------------------------------------------------------------
class SileroTTSEngine(BaseTTSEngine):
    """Silero TTS в долгоживущем процессе-воркере.

    Воркер (``facegate/tts/silero_worker.py``, официальный пакет ``silero``)
    загружает модель один раз и дальше принимает по stdin JSON-запросы
    ``{"op":"synth",...}``, отвечая ``{"ok":true}`` / ``{"ok":false,"error":...}``
    в stdout. При падении или таймауте воркер перезапускается автоматически.
    """

    name = "silero"

    def __init__(self, startup_timeout=None, python_exe=None, worker_cmd=None):
        self.startup_timeout = float(startup_timeout or
                                     os.environ.get("SILERO_STARTUP_TIMEOUT", "240"))
        self.python_exe = python_exe or sys.executable
        # worker_cmd — переопределение команды воркера (используется в тестах)
        self.worker_cmd = worker_cmd
        self.model_info = None
        self.speakers = tuple(SILERO_SPEAKERS)
        self.last_error = None
        self._lock = threading.Lock()
        self._proc = None
        self._responses = None
        self._reader = None
        self._stderr_tail = ""

    # ---------- доступность ----------
    @staticmethod
    def _have_module(mod):
        try:
            return importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):  # pragma: no cover
            return False

    def available(self):
        # официальный пакет silero + torch (scipy проверяем как runtime-зависимость модели)
        return (self._have_module("silero") and self._have_module("torch")
                and self._have_module("scipy"))

    # ---------- воркер ----------
    def _ensure_worker(self):
        if self._proc is not None and self._proc.poll() is None and self._responses is not None:
            return
        self._kill_worker()
        if self.worker_cmd is None and not self.available():
            raise TTSError(
                "Silero TTS недоступен: не установлены пакеты silero/torch/scipy. "
                "В Docker они входят в образ сервера; на хосте: "
                "pip install --index-url https://download.pytorch.org/whl/cpu torch && "
                "pip install silero scipy  (или ./install.sh --tts)")
        cmd = self.worker_cmd or [self.python_exe, "-u", "-m", "facegate.tts.silero_worker"]
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        except OSError as e:
            raise TTSError(f"Не удалось запустить процесс Silero TTS: {e}") from e
        self._responses = queue.Queue()
        self._stderr_tail = ""
        self._reader = threading.Thread(target=self._read_stdout, daemon=True,
                                        name="silero-reader")
        self._reader.start()
        threading.Thread(target=self._drain_stderr, daemon=True,
                         name="silero-stderr").start()
        try:
            hello = self._responses.get(timeout=self.startup_timeout)
        except queue.Empty:
            self._kill_worker()
            raise TTSError(
                f"Silero TTS не ответил за {self.startup_timeout:.0f} c "
                f"(загрузка модели){self._stderr_suffix()}")
        if hello is None:
            self._kill_worker()
            raise TTSError(
                "Процесс Silero TTS завершился при загрузке модели"
                " (возможна нехватка памяти: silero/torch нужно ~600 МБ ОЗУ)"
                f"{self._stderr_suffix()}")
        if not hello.get("ready"):
            self._kill_worker()
            raise TTSError(f"Silero TTS не инициализировался: {hello.get('error')}"
                           f"{self._stderr_suffix()}")
        self.model_info = hello.get("model")
        self.speakers = tuple(hello.get("speakers") or SILERO_SPEAKERS)
        log.info("Silero TTS готов (модель: %s, голоса: %s)",
                 self.model_info, ", ".join(self.speakers))

    def _stderr_suffix(self):
        return f" | stderr: {self._stderr_tail[-300:]}" if self._stderr_tail else ""

    def _read_stdout(self):
        proc, responses = self._proc, self._responses
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    responses.put(json.loads(line))
                except ValueError:
                    log.debug("Silero worker: не-JSON строка: %s", line[:120])
        except (ValueError, OSError):  # pragma: no cover - закрытие pipe при kill
            pass
        finally:
            responses.put(None)  # EOF — воркер больше не ответит

    def _drain_stderr(self):
        proc = self._proc
        try:
            for line in proc.stderr:
                self._stderr_tail = (self._stderr_tail + line)[-4000:]
        except (ValueError, OSError):  # pragma: no cover
            pass

    def _kill_worker(self):
        proc, self._proc = self._proc, None
        self._responses = None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5.0)
            except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
                pass

    def _request(self, payload, timeout):
        proc = self._proc
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise TTSError(f"Процесс Silero TTS недоступен: {e}") from e
        try:
            resp = self._responses.get(timeout=timeout)
        except queue.Empty:
            self._kill_worker()
            raise TTSError(f"Таймаут синтеза Silero TTS ({timeout:.0f} c)")
        if resp is None:
            rc = proc.returncode if proc is not None else None
            self._kill_worker()
            hint = ""
            if rc is not None and rc < 0:
                hint = (f" (процесс убит сигналом {-rc}"
                        + " — вероятно, нехватка памяти: silero/torch нужно ~600 МБ ОЗУ"
                        if -rc == 9 else "") + ")"
            raise TTSError(f"Процесс Silero TTS завершился{hint}{self._stderr_suffix()}")
        return resp

    # ---------- синтез ----------
    def synthesize(self, text, out_path, voice=None, rate=None, speed=None,
                   timeout=120.0):
        # Голос передаётся как есть: воркер сверит его со списком голосов
        # модели и при промахе возьмёт голос по умолчанию.
        speaker = str(voice or "").strip().lower() or SILERO_DEFAULT_SPEAKER
        try:
            speed = float(speed) if speed else 1.0
        except (TypeError, ValueError):
            speed = 1.0
        speed = min(2.0, max(0.5, speed))
        with self._lock:
            self._ensure_worker()
            resp = self._request({
                "op": "synth", "text": text, "out": out_path,
                "speaker": speaker, "speed": speed,
                "sample_rate": SILERO_SAMPLE_RATE,
                "fallback_sample_rate": SILERO_FALLBACK_SAMPLE_RATE,
                "pad_ms": SILERO_PAD_MS,
            }, timeout=timeout)
            if not resp.get("ok"):
                self.last_error = resp.get("error")
                raise TTSError(f"Silero TTS: {self.last_error}")
        verify_wav(out_path, text, engine=self.name)

    def close(self):
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
                    proc.stdin.flush()
                    proc.wait(timeout=3.0)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
            self._kill_worker()


def verify_wav(path, text="", engine="tts"):
    """Проверяет результат синтеза: файл читаемый, непустой, фраза не обрезана.

    Возвращает длительность в секундах (или 0.0). Ошибки не бросает — это
    диагностика: раньше «короткий» WAV молча уезжал клиенту, и фраза звучала
    наполовину.
    """
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate() or 0
            frames = w.getnframes()
    except (OSError, wave.Error) as e:
        log.warning("%s: результат не является корректным WAV (%s): %s", engine, path, e)
        return 0.0
    if not rate or frames <= 0:
        log.warning("%s: пустое аудио (%s)", engine, path)
        return 0.0
    seconds = frames / float(rate)
    if looks_truncated(text, seconds):
        log.warning("%s: аудио подозрительно короткое (%.2f c для %d символов) — "
                    "фраза «%s» могла быть обрезана моделью",
                    engine, seconds, len(text or ""), (text or "")[:60])
    return seconds


# ---------------------------------------------------------------------------
# espeak-ng / espeak (системный, лёгкий запасной вариант)
# ---------------------------------------------------------------------------
class EspeakTTSEngine(BaseTTSEngine):
    """espeak-ng/espeak через CLI. Язык по умолчанию — ``ru``.

    В отличие от pyttsx3, не требует dbus/python-обвязок: только бинарник,
    который пишет WAV напрямую (``-w``).
    """

    name = "espeak"

    def __init__(self, binary=None, default_voice="ru"):
        self._binary = binary
        self.default_voice = default_voice

    def _bin(self):
        return self._binary or shutil.which("espeak-ng") or shutil.which("espeak")

    def available(self):
        return bool(self._bin())

    def synthesize(self, text, out_path, voice=None, rate=None, speed=None,
                   timeout=60.0):
        binary = self._bin()
        if not binary:
            raise TTSError("espeak-ng не найден (sudo apt install espeak-ng)")
        cmd = [binary, "-v", str(voice or self.default_voice), "-w", out_path]
        try:
            if rate:
                cmd += ["-s", str(int(rate))]
        except (TypeError, ValueError):
            pass
        tmp = None
        try:
            # текст передаём файлом: надёжнее для кириллицы/спецсимволов,
            # чем аргументом командной строки
            fd, tmp = tempfile.mkstemp(prefix="tts_text_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(normalize_for_tts(text))
            cmd += ["-f", tmp]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if proc.returncode != 0:
                raise TTSError(f"espeak завершился с кодом {proc.returncode}: "
                               f"{(proc.stderr or '')[-200:]}")
        except subprocess.TimeoutExpired:
            raise TTSError(f"Таймаут синтеза espeak ({timeout:.0f} c)") from None
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        verify_wav(out_path, text, engine=self.name)


# ---------------------------------------------------------------------------
# pyttsx3 (SAPI5 / NSSpeech / espeak через python-обвязку)
# ---------------------------------------------------------------------------
PYTTSX3_SUBPROCESS_CODE = r"""
import sys
import pyttsx3

text, out_path = sys.argv[1], sys.argv[2]
rate = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else None
hint = sys.argv[4] if len(sys.argv) > 4 else None

engine = pyttsx3.init()
if rate:
    engine.setProperty("rate", rate)
if hint:
    h = hint.lower()
    for v in engine.getProperty("voices"):
        ident = str(getattr(v, "id", ""))
        name = str(getattr(v, "name", ""))
        if h in ident.lower() or h in name.lower():
            engine.setProperty("voice", v.id)
            break
engine.save_to_file(text, out_path)
engine.runAndWait()
"""


class Pyttsx3TTSEngine(BaseTTSEngine):
    """pyttsx3 в разовом субпроцессе (SAPI5 в Windows, NSSpeech в macOS)."""

    name = "pyttsx3"

    @staticmethod
    def available():
        try:
            return importlib.util.find_spec("pyttsx3") is not None
        except (ImportError, ValueError):  # pragma: no cover
            return False

    def synthesize(self, text, out_path, voice=None, rate=None, speed=None,
                   timeout=60.0):
        if not self.available():
            raise TTSError("pyttsx3 не установлен")
        cmd = [sys.executable, "-c", PYTTSX3_SUBPROCESS_CODE,
               normalize_for_tts(text), out_path,
               str(rate) if rate else "", str(voice or "")]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TTSError(f"Таймаут синтеза pyttsx3 ({timeout:.0f} c)") from None
        if proc.returncode != 0:
            raise TTSError(f"pyttsx3 завершился с кодом {proc.returncode}: "
                           f"{(proc.stderr or '')[-300:]}")
        verify_wav(out_path, text, engine=self.name)


# ---------------------------------------------------------------------------
# Фабрика
# ---------------------------------------------------------------------------
ENGINE_CLASSES = {
    "silero": SileroTTSEngine,
    "espeak": EspeakTTSEngine,
    "pyttsx3": Pyttsx3TTSEngine,
}
AUTO_ORDER = ("silero", "espeak", "pyttsx3")


def list_available_engines():
    """Какие движки доступны в текущем окружении: {имя: bool}."""
    return {name: cls().available() for name, cls in ENGINE_CLASSES.items()}


def detect_engine(pref="auto"):
    """Имя движка, который будет использован (для показа в админ-панели)."""
    pref = (pref or "auto").strip().lower()
    if pref in ENGINE_CLASSES:
        return pref
    for name in AUTO_ORDER:
        if ENGINE_CLASSES[name]().available():
            return name
    return "не найден"


def get_engine(pref="auto"):
    """Создаёт экземпляр движка по предпочтению.

    ``auto`` — первый доступный из silero → espeak → pyttsx3 (или None, если
    нет ничего). Явное имя возвращается даже если движок недоступен — ошибка
    проявится при первом синтезе с понятным сообщением.
    """
    pref = (pref or "auto").strip().lower()
    if pref in ENGINE_CLASSES:
        return ENGINE_CLASSES[pref]()
    for name in AUTO_ORDER:
        engine = ENGINE_CLASSES[name]()
        if engine.available():
            return engine
    return None
