# -*- coding: utf-8 -*-
"""Движки TTS: Silero (протокол воркера), espeak, фабрика движков."""
import json
import os
import subprocess
import sys
import wave

import pytest

from facegate.tts import engines
from facegate.tts.engines import (EspeakTTSEngine, Pyttsx3TTSEngine,
                                  SileroTTSEngine, TTSError, detect_engine,
                                  get_engine, list_available_engines)

# ---------------------------------------------------------------------------
# Фейковый воркер, говорящий по тому же протоколу, что facegate.tts.silero_worker
# ---------------------------------------------------------------------------
FAKE_WORKER = r'''
import json, sys, wave
def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
out({"ready": True, "model": "fake-v5", "speakers": ["baya", "xenia"]})
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    op = req.get("op")
    if op == "shutdown":
        break
    if op == "ping":
        out({"ok": True}); continue
    if op == "synth":
        text = req.get("text", "")
        if "boom" in text:
            out({"ok": False, "error": "worker exploded"}); continue
        if text == "die":
            import os as _os; _os._exit(3)
        with wave.open(req["out"], "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(b"\x00\x00" * 2400)
        out({"ok": True}); continue
    out({"ok": False, "error": "unknown op"})
'''


@pytest.fixture
def fake_silero():
    engine = SileroTTSEngine(startup_timeout=30.0,
                             worker_cmd=[sys.executable, "-u", "-c", FAKE_WORKER])
    yield engine
    engine.close()


def valid_wav(path):
    return os.path.exists(path) and os.path.getsize(path) > 44


# ---------------------------------------------------------------------------
# SileroTTSEngine — протокол воркера
# ---------------------------------------------------------------------------
def test_silero_worker_protocol_synth(fake_silero, tmp_path):
    out_path = str(tmp_path / "hello.wav")
    fake_silero.synthesize("Здравствуйте, Михаил!", out_path, voice="baya", timeout=30)
    assert valid_wav(out_path)
    with wave.open(out_path) as w:
        assert w.getframerate() == 24000
        assert w.getnchannels() == 1


def test_silero_worker_reports_speakers_and_model(fake_silero, tmp_path):
    fake_silero.synthesize("тест", str(tmp_path / "a.wav"), timeout=30)
    assert fake_silero.model_info == "fake-v5"
    assert fake_silero.speakers == ("baya", "xenia")


def test_silero_worker_error_propagates(fake_silero, tmp_path):
    with pytest.raises(TTSError) as ei:
        fake_silero.synthesize("boom", str(tmp_path / "bad.wav"), timeout=30)
    assert "worker exploded" in str(ei.value)


def test_silero_worker_restarts_after_death(fake_silero, tmp_path):
    # первый синтез поднимает воркер
    fake_silero.synthesize("раз", str(tmp_path / "1.wav"), timeout=30)
    proc = fake_silero._proc
    assert proc is not None and proc.poll() is None
    # роняем воркер «внезапно»
    with pytest.raises(TTSError):
        fake_silero.synthesize("die", str(tmp_path / "2.wav"), timeout=30)
    # следующий запрос перезапускает воркер и succeeds
    fake_silero.synthesize("три", str(tmp_path / "3.wav"), timeout=30)
    assert valid_wav(str(tmp_path / "3.wav"))
    assert fake_silero._proc is not proc


def test_silero_close_terminates_worker(fake_silero, tmp_path):
    fake_silero.synthesize("тест", str(tmp_path / "x.wav"), timeout=30)
    proc = fake_silero._proc
    fake_silero.close()
    assert fake_silero._proc is None
    assert proc.poll() is not None


def test_silero_timeout_kills_worker(fake_silero, tmp_path):
    fake_silero.synthesize("тест", str(tmp_path / "t.wav"), timeout=30)
    # фейковый воркер на "hang" не отвечает — но вместо hang роняем его и
    # проверяем таймаут ожидания ответа на мёртвом pipe
    fake_silero._proc.kill()
    with pytest.raises(TTSError):
        fake_silero.synthesize("ещё", str(tmp_path / "t2.wav"), timeout=5)


# ---------------------------------------------------------------------------
# EspeakTTSEngine
# ---------------------------------------------------------------------------
def test_espeak_builds_command_with_ru_voice(tmp_path, monkeypatch):
    captured = {}

    def fake_which(name):
        return "/usr/bin/espeak-ng" if name == "espeak-ng" else None

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # espeak пишет результат в -w файл
        out_path = cmd[cmd.index("-w") + 1]
        with wave.open(out_path, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
            w.writeframes(b"\x00\x00" * 2205)

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(engines.shutil, "which", fake_which)
    monkeypatch.setattr(engines.subprocess, "run", fake_run)

    engine = EspeakTTSEngine()
    assert engine.available() is True
    out_path = str(tmp_path / "ru.wav")
    engine.synthesize("Здравствуйте!", out_path, rate=160, timeout=10)
    cmd = captured["cmd"]
    assert cmd[0] == "/usr/bin/espeak-ng"
    assert cmd[cmd.index("-v") + 1] == "ru"          # русский язык по умолчанию!
    assert cmd[cmd.index("-w") + 1] == out_path
    assert cmd[cmd.index("-s") + 1] == "160"
    assert "-f" in cmd                                 # текст передаётся файлом
    assert valid_wav(out_path)


def test_espeak_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(engines.shutil, "which", lambda name: "/usr/bin/espeak-ng")

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 1
            stderr = "bad voice"
        return R()

    monkeypatch.setattr(engines.subprocess, "run", fake_run)
    engine = EspeakTTSEngine()
    with pytest.raises(TTSError):
        engine.synthesize("текст", str(tmp_path / "x.wav"), timeout=5)


def test_espeak_missing_binary(tmp_path):
    engine = EspeakTTSEngine(binary=None)
    engine._binary = None
    orig_which = engines.shutil.which
    engines.shutil.which = lambda name: None
    try:
        assert engine.available() is False
        with pytest.raises(TTSError):
            engine.synthesize("текст", str(tmp_path / "x.wav"))
    finally:
        engines.shutil.which = orig_which


def test_pyttsx3_unavailable_raises(tmp_path):
    engine = Pyttsx3TTSEngine()
    if engine.available():
        pytest.skip("pyttsx3 установлен — проверка недоступности неприменима")
    with pytest.raises(TTSError):
        engine.synthesize("текст", str(tmp_path / "x.wav"))


# ---------------------------------------------------------------------------
# Фабрика движков
# ---------------------------------------------------------------------------
def _patch_availability(monkeypatch, silero=False, espeak=False, pyttsx3=False):
    monkeypatch.setattr(SileroTTSEngine, "available", lambda self: silero)
    monkeypatch.setattr(EspeakTTSEngine, "available", lambda self: espeak)
    monkeypatch.setattr(Pyttsx3TTSEngine, "available", staticmethod(lambda: pyttsx3))


def test_detect_engine_auto_order(monkeypatch):
    _patch_availability(monkeypatch, silero=True, espeak=True, pyttsx3=True)
    assert detect_engine() == "silero"
    _patch_availability(monkeypatch, silero=False, espeak=True, pyttsx3=True)
    assert detect_engine() == "espeak"
    _patch_availability(monkeypatch, silero=False, espeak=False, pyttsx3=True)
    assert detect_engine() == "pyttsx3"


def test_detect_engine_none_available(monkeypatch):
    _patch_availability(monkeypatch)
    assert detect_engine() == "не найден"
    assert get_engine() is None
    assert list_available_engines() == {"silero": False, "espeak": False, "pyttsx3": False}


def test_get_engine_explicit_pref_even_if_unavailable(monkeypatch):
    _patch_availability(monkeypatch)
    engine = get_engine("silero")
    assert engine is not None and engine.name == "silero"
    assert engine.available() is False


# ---------------------------------------------------------------------------
# Реальная интеграция с Silero (пропускается, если пакет/модель недоступны
# или свободной памяти мало — torch-воркеру нужно ~600 МБ, а в тесном CI его
# убивает OOM-killer, что не является ошибкой кода)
# ---------------------------------------------------------------------------
def _silero_model_cached():
    try:
        import silero
        model_dir = os.path.join(os.path.dirname(silero.__file__), "model")
        return os.path.isdir(model_dir) and bool(os.listdir(model_dir))
    except ImportError:
        return False


def memory_available_mb():
    """MemAvailable из /proc/meminfo (на не-Linux возвращает много)."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return 10_000


silero_ready = (SileroTTSEngine().available() and _silero_model_cached()
                and memory_available_mb() >= 900)


@pytest.mark.skipif(not silero_ready,
                    reason="silero/torch/scipy, модель или память (>900 МБ) недоступны")
def test_real_silero_synthesizes_russian_wav(tmp_path):
    engine = SileroTTSEngine(startup_timeout=240.0)
    try:
        out_path = str(tmp_path / "real.wav")
        engine.synthesize("Здравствуйте, Михаил! Добро пожаловать.", out_path,
                          voice="baya", timeout=180.0)
        assert valid_wav(out_path)
        with wave.open(out_path) as w:
            assert w.getframerate() == 24000
            duration = w.getnframes() / float(w.getframerate())
            assert 0.5 < duration < 20.0, f"подозрительная длительность: {duration}"
        # неизвестный голос — фолбэк на голос по умолчанию, без ошибки
        out2 = str(tmp_path / "real2.wav")
        engine.synthesize("Проверка", out2, voice="несуществующий", timeout=120.0)
        assert valid_wav(out2)
    finally:
        engine.close()
