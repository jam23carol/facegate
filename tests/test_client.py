# -*- coding: utf-8 -*-
import base64
import io
import os
import time
import wave

import client
from client import (handle_command, normalize_wav, play_wav_bytes,
                    player_candidates, resolve_player)


def test_resolve_player_none_available(monkeypatch):
    monkeypatch.setattr(client.shutil, "which", lambda name: None)
    # встроенный плеер платформы (winsound в Windows) тоже считаем недоступным,
    # чтобы тест был платформенно-независимым
    monkeypatch.setattr(client, "native_player", lambda: None)
    assert resolve_player(None) is None
    assert player_candidates(None) == []


def test_resolve_player_first_available(monkeypatch):
    monkeypatch.setattr(client, "native_player", lambda: None)
    monkeypatch.setattr(client.shutil, "which",
                        lambda name: "/usr/bin/aplay" if name == "aplay" else None)
    assert resolve_player(None) == ["aplay", "-q"]


def test_resolve_player_explicit_cmd(monkeypatch):
    monkeypatch.setattr(client, "native_player", lambda: None)
    monkeypatch.setattr(client.shutil, "which",
                        lambda name: "/usr/bin/mpv" if name == "mpv" else None)
    assert resolve_player("mpv --really-quiet") == ["mpv", "--really-quiet"]


def test_resolve_player_windows_uses_winsound(monkeypatch):
    """В Windows звук работает без внешних утилит (winsound входит в Python)."""
    monkeypatch.setattr(client.shutil, "which", lambda name: None)
    monkeypatch.setattr(client, "native_player", lambda: ["@winsound"])
    assert resolve_player(None) == ["@winsound"]
    assert player_candidates(None)[0] == ["@winsound"]


def test_resolve_player_explicit_missing_falls_back(monkeypatch):
    monkeypatch.setattr(client, "native_player", lambda: None)
    calls = []
    def fake_which(name):
        calls.append(name)
        return "/usr/bin/aplay" if name == "aplay" else None
    monkeypatch.setattr(client.shutil, "which", fake_which)
    result = resolve_player("nosuchplayer42")
    assert result == ["aplay", "-q"]
    assert "nosuchplayer42" in calls


def test_play_wav_bytes_runs_player(monkeypatch, wav_bytes, tmp_path):
    """Плеер запускается, а WAV перед ним приводится к 16 бит/моно/48 кГц."""
    launched = {}

    class FakePopen:
        returncode = 0

        def __init__(self, cmd, **kwargs):
            launched["cmd"] = cmd

        def communicate(self, timeout=None):
            launched["waited"] = True          # плеер доиграл до конца
            return "", ""

        def kill(self):
            launched["killed"] = True

    tmpdir = str(tmp_path / "client_wav")
    os.makedirs(tmpdir, exist_ok=True)
    monkeypatch.setattr(client.tempfile, "mkstemp",
                        lambda suffix=None, prefix=None, **kw: (os.open(
                            os.path.join(tmpdir, "g.wav"),
                            os.O_CREAT | os.O_WRONLY | os.O_TRUNC),
                            os.path.join(tmpdir, "g.wav")))
    monkeypatch.setattr(client.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(client.shutil, "which",
                        lambda name: "/usr/bin/paplay" if name == "paplay" else None)
    monkeypatch.setattr(client, "native_player", lambda: None)
    client._PLAYER = None                      # чистый плеер без очереди прошлых тестов

    ok = play_wav_bytes(wav_bytes, player_cmd=None)
    assert ok is True
    # воспроизведение идёт в потоке очереди — ждём фактического запуска плеера
    deadline = time.time() + 5.0
    while "cmd" not in launched and time.time() < deadline:
        time.sleep(0.01)
    assert launched["cmd"][0] == "paplay"
    tmp_file = launched["cmd"][-1]
    with open(tmp_file, "rb") as f:
        written = f.read()
    assert len(written) > 44
    with wave.open(io.BytesIO(written)) as w:
        # исходный fixture — 22050 Гц; клиент приводит его к аппаратной частоте
        assert w.getframerate() == client.TARGET_SAMPLE_RATE
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
    os.remove(tmp_file)


def test_play_wav_bytes_waits_for_player(monkeypatch, wav_bytes):
    """Плеер доигрывает до конца: процесс ждём, а не бросаем (обрезанный хвост)."""
    state = {}

    class SlowPopen:
        returncode = 0

        def __init__(self, cmd, **kwargs):
            state["cmd"] = cmd

        def communicate(self, timeout=None):
            time.sleep(0.05)
            state["waited"] = True
            return "", ""

        def kill(self):
            state["killed"] = True

    monkeypatch.setattr(client.subprocess, "Popen", SlowPopen)
    monkeypatch.setattr(client.shutil, "which",
                        lambda name: "/usr/bin/paplay" if name == "paplay" else None)
    monkeypatch.setattr(client, "native_player", lambda: None)
    client._PLAYER = None
    assert play_wav_bytes(wav_bytes) is True
    deadline = time.time() + 5.0
    while not state.get("waited") and time.time() < deadline:
        time.sleep(0.01)
    assert state.get("waited") is True
    assert "killed" not in state


def test_play_wav_bytes_falls_back_to_next_player(monkeypatch, wav_bytes):
    """Первый плеер упал (как aplay на 24 кГц) — звук играет следующий."""
    attempts = []

    class FailingPopen:
        def __init__(self, cmd, **kwargs):
            attempts.append(cmd[0])
            self.returncode = 0 if cmd[0] == "mpv" else 1

        def communicate(self, timeout=None):
            return "", "" if self.returncode == 0 else "Rate 24000Hz not supported"

        def kill(self):
            pass

    monkeypatch.setattr(client.subprocess, "Popen", FailingPopen)
    monkeypatch.setattr(client.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in ("aplay", "mpv") else None)
    monkeypatch.setattr(client, "native_player", lambda: None)
    client._PLAYER = None
    assert play_wav_bytes(wav_bytes) is True
    deadline = time.time() + 5.0
    while "mpv" not in attempts and time.time() < deadline:
        time.sleep(0.01)
    assert "aplay" in attempts and "mpv" in attempts


def test_play_wav_bytes_dedupes_repeats(monkeypatch, wav_bytes):
    """Один и тот же звук подряд не играется дважды (сервер шлёт повторы)."""
    played = []

    class CountingPopen:
        returncode = 0

        def __init__(self, cmd, **kwargs):
            played.append(cmd[0])

        def communicate(self, timeout=None):
            return "", ""

        def kill(self):
            pass

    monkeypatch.setattr(client.subprocess, "Popen", CountingPopen)
    monkeypatch.setattr(client.shutil, "which",
                        lambda name: "/usr/bin/paplay" if name == "paplay" else None)
    monkeypatch.setattr(client, "native_player", lambda: None)
    client._PLAYER = None
    assert play_wav_bytes(wav_bytes) is True
    time.sleep(0.2)
    assert play_wav_bytes(wav_bytes) is True      # дубль — принят, но не играется
    time.sleep(0.2)
    assert len(played) == 1


def test_normalize_wav_resamples_24k_to_target():
    """24 кГц (Silero) → 48 кГц: длительность сохраняется, формат — 16 бит моно."""
    src_rate, seconds = 24000, 1.0
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(src_rate)
        w.writeframes(b"\x00\x10" * int(src_rate * seconds))
    data, info = normalize_wav(buf.getvalue(), target_rate=48000, pad_ms=0)
    assert info["converted"] is True and info["rate"] == src_rate
    with wave.open(io.BytesIO(data)) as w:
        assert w.getframerate() == 48000
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        duration = w.getnframes() / float(w.getframerate())
        # без передискретизации aplay играл бы этот файл вдвое быстрее
        assert abs(duration - seconds) < 0.05


def test_normalize_wav_pads_tail_and_mixes_stereo():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x10\x00\x10" * 4800)      # 0.1 c стерео
    data, info = normalize_wav(buf.getvalue(), pad_ms=200)
    with wave.open(io.BytesIO(data)) as w:
        assert w.getnchannels() == 1
        duration = w.getnframes() / float(w.getframerate())
        assert 0.25 < duration < 0.45                      # 0.1 + хвост тишины
    assert info["seconds"] > 0.2


def test_normalize_wav_keeps_non_wav_payload():
    data, info = normalize_wav(b"ID3 not a wav")
    assert data == b"ID3 not a wav"
    assert info["error"]


def test_play_wav_bytes_no_player(monkeypatch, wav_bytes):
    monkeypatch.setattr(client.shutil, "which", lambda name: None)
    monkeypatch.setattr(client, "native_player", lambda: None)
    client._PLAYER = None
    assert play_wav_bytes(wav_bytes) is False


def test_handle_command_greet_with_audio(monkeypatch, wav_bytes):
    received = {}

    def fake_play(data, player_cmd=None):
        received["data"] = data
        received["player"] = player_cmd
        return True

    monkeypatch.setattr(client, "play_wav_bytes", fake_play)
    msg = {
        "action": "greet",
        "name": "Михаил",
        "text": "Здравствуйте, Михаил!",
        "audio_b64": base64.b64encode(wav_bytes).decode("ascii"),
    }
    result = handle_command(msg, play=True, player_cmd=["aplay"])
    assert result["played"] is True
    assert received["data"] == wav_bytes


def test_handle_command_greet_without_audio():
    msg = {"action": "greet", "name": "Анна", "text": "Здравствуйте, Анна!",
           "audio_b64": None}
    result = handle_command(msg, play=True)
    assert result["played"] is False


def test_handle_command_audio_skipped_when_disabled(monkeypatch, wav_bytes):
    called = {"n": 0}

    def fake_play(data, player_cmd=None):
        called["n"] += 1
        return True

    monkeypatch.setattr(client, "play_wav_bytes", fake_play)
    msg = {"action": "greet", "name": "X", "audio_b64":
           base64.b64encode(wav_bytes).decode("ascii")}
    handle_command(msg, play=False)
    assert called["n"] == 0


def test_handle_command_stop_clears_queue(monkeypatch):
    stopped = []
    monkeypatch.setattr(client, "stop_audio", lambda: stopped.append(True))
    result = handle_command({"action": "stop"})
    assert result["stopped"] is True and stopped == [True]


def test_handle_command_unknown_action():
    result = handle_command({"action": "custom_cmd", "x": 1})
    assert result["action"] == "custom_cmd"

# ---------- автоопределение камеры ----------
class FakeCap:
    """Заглушка cv2.VideoCapture: открывается/читает по списку «рабочих» источников."""
    opened = []

    def __init__(self, source):
        self.source = source
        self._ok = source in FakeCap.opened

    def isOpened(self):
        return self._ok

    def read(self):
        if not self._ok:
            return False, None
        import numpy as np
        return True, np.zeros((8, 8, 3), dtype=np.uint8)

    def release(self):
        pass


def test_autodetect_skips_dead_devices(monkeypatch):
    monkeypatch.setattr(client, "video_devices",
                        lambda: ["/dev/video0", "/dev/video1", "/dev/video2"])
    FakeCap.opened = ["/dev/video2"]          # video0/1 — узлы метаданных, не читаются
    monkeypatch.setattr(client.cv2, "VideoCapture", FakeCap)
    source, cap = client.autodetect_camera()
    assert source == "/dev/video2"
    assert cap is not None


def test_autodetect_none_found(monkeypatch):
    monkeypatch.setattr(client, "video_devices", lambda: [])
    FakeCap.opened = []
    monkeypatch.setattr(client.cv2, "VideoCapture", FakeCap)
    source, cap = client.autodetect_camera(max_index=3)
    assert source is None and cap is None


def test_open_capture_auto_sets_camera_id(monkeypatch):
    args = client.parse_args(["--camera-id", "auto"])
    monkeypatch.setattr(client, "autodetect_camera", lambda **kw: ("/dev/video1", FakeCap("/dev/video1")))
    FakeCap.opened = ["/dev/video1"]
    cap = client.open_capture(args)
    assert cap is not None
    assert args.camera_id == "/dev/video1"    # мета данные получат реальное устройство


def test_open_capture_explicit_index(monkeypatch):
    args = client.parse_args(["--camera-id", "0"])
    FakeCap.opened = [0]
    monkeypatch.setattr(client.cv2, "VideoCapture", FakeCap)
    cap = client.open_capture(args)
    assert cap is not None and args.camera_id == 0


def test_parse_args_camera_id_from_env(monkeypatch):
    monkeypatch.setenv("CAMERA_ID", "3")
    args = client.parse_args([])
    assert args.camera_id == "3"
    monkeypatch.delenv("CAMERA_ID")
    assert client.parse_args([]).camera_id == "auto"


# ---------- автовыбор аудиовыхода ----------
def test_resolve_player_prefers_paplay_with_live_pulse_socket(monkeypatch):
    monkeypatch.setattr(client, "native_player", lambda: None)
    monkeypatch.setattr(client, "pulse_available", lambda: True)
    monkeypatch.setattr(client, "alsa_available", lambda: True)
    monkeypatch.setattr(client.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in ("paplay", "aplay") else None)
    assert resolve_player(None) == ["paplay"]


def test_resolve_player_falls_back_to_alsa(monkeypatch):
    monkeypatch.setattr(client, "native_player", lambda: None)
    monkeypatch.setattr(client, "pulse_available", lambda: False)
    monkeypatch.setattr(client, "alsa_available", lambda: True)
    monkeypatch.setattr(client.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in ("paplay", "aplay") else None)
    # сокет PulseAudio мёртв — paplay играть не сможет, берём aplay
    assert resolve_player(None) == ["aplay", "-q"]
