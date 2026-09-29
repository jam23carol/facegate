# -*- coding: utf-8 -*-
import base64
import json

import numpy as np
import pytest
import zmq

from facegate.commands import build_greet_command, send_command
from facegate.server import (LatestFrames, recognize_face,
                    scale_box)


def vec(first):
    v = np.zeros(128, dtype=np.float32)
    v[0] = first
    return v


ENTRIES = [("Анна", vec(0.1)), ("Михаил", vec(0.5))]


def test_recognize_match(fake_fr):
    name, dist = recognize_face(ENTRIES, vec(0.11), threshold=0.6)
    assert name == "Анна"
    assert dist < 0.6


def test_recognize_unknown(fake_fr):
    name, dist = recognize_face(ENTRIES, vec(0.48), threshold=0.6)
    assert name == "Михаил"  # ближе к Михаилу


def test_recognize_above_threshold_returns_none(fake_fr):
    name, dist = recognize_face(ENTRIES, vec(3.0), threshold=0.6)
    assert name is None
    assert dist > 0.6


def test_recognize_empty_entries(fake_fr):
    name, dist = recognize_face([], vec(0.0), threshold=0.6)
    assert name is None
    assert dist == 1.0


def test_build_greet_command_with_audio(wav_bytes):
    payload = build_greet_command("Михаил", wav_bytes, "Здравствуйте, {name}!")
    assert payload["action"] == "greet"
    assert payload["name"] == "Михаил"
    assert payload["text"] == "Здравствуйте, Михаил!"
    assert base64.b64decode(payload["audio_b64"]) == wav_bytes


def test_build_greet_command_without_audio():
    payload = build_greet_command("Анна", None, "Здравствуйте, {name}!")
    assert payload["audio_b64"] is None
    assert payload["text"] == "Здравствуйте, Анна!"


def test_send_command_zmq_roundtrip(wav_bytes):
    ctx = zmq.Context()
    try:
        pub = ctx.socket(zmq.PUB)
        port = pub.bind_to_random_port("tcp://127.0.0.1")
        sub = ctx.socket(zmq.SUB)
        sub.connect(f"tcp://127.0.0.1:{port}")
        sub.setsockopt_string(zmq.SUBSCRIBE, "clientX")
        import time
        time.sleep(0.3)

        payload = build_greet_command("Михаил", wav_bytes, "Здравствуйте, {name}!")
        send_command(pub, "clientX", payload)

        if not sub.poll(3000):
            pytest.fail("Команда не доставлена по PUB/SUB")
        topic = sub.recv_string()
        body = json.loads(sub.recv_string())
        assert topic == "clientX"
        assert body["action"] == "greet"
        assert body["name"] == "Михаил"
        assert base64.b64decode(body["audio_b64"]) == wav_bytes
    finally:
        pub.close(0)
        sub.close(0)
        ctx.term()


def test_scale_box_identity():
    assert scale_box((10, 50, 90, 20), 1.0) == (10, 50, 90, 20)


def test_scale_box_upscales_to_original_frame():
    # кадр 1280px уменьшили до 640px (factor 2) — координаты нужно вернуть обратно
    assert scale_box((5, 100, 95, 10), 2.0) == (10, 200, 190, 20)


def test_scale_box_rounds():
    assert scale_box((1, 2, 3, 4), 1.5) == (2, 3, 4, 6)


def test_latest_frames_keeps_only_newest():
    import numpy as np
    q = LatestFrames()
    q.put("cam1", np.zeros((2, 2, 3), dtype=np.uint8))
    q.put("cam1", np.ones((2, 2, 3), dtype=np.uint8))
    q.put("cam2", np.zeros((2, 2, 3), dtype=np.uint8))
    batch = q.take_all(timeout=0.1)
    assert sorted(batch) == ["cam1", "cam2"]
    assert batch["cam1"].sum() > 0        # взят последний кадр
    assert q.take_all(timeout=0.05) == {} # очередь пуста
    assert len(q) == 0


def test_latest_frames_wake_unblocks():
    import threading
    q = LatestFrames()
    done = threading.Event()

    def waiter():
        q.take_all(timeout=5.0)
        done.set()

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    q.wake()
    assert done.wait(timeout=1.0) is True


# ---------- персональная статистика приветствий ----------
def test_stats_greets_by_person():
    from facegate.stats import Stats
    s = Stats()
    s.incr_greet("Анна")
    s.incr_greet("Анна")
    s.incr_greet("Марк")
    assert s.counters()["greets_sent"] == 3          # общий счётчик не сломан
    assert s.greets_by_person() == {"Анна": 2, "Марк": 1}
    assert s.greet_count("Анна") == 2
    assert s.greet_count("Нету") == 0
    snap = s.snapshot()
    assert snap["greets_by_person"]["Марк"] == 1


def test_stats_rename_and_forget_person():
    from facegate.stats import Stats
    s = Stats()
    s.incr_greet("Анна")
    s.incr_greet("Марк")
    s.rename_person("Анна", "Анна П.")               # статистика переносится
    assert s.greet_count("Анна") == 0
    assert s.greet_count("Анна П.") == 1
    s.incr_greet("Анна П.")
    s.rename_person("Марк", "Анна П.")               # слияние при коллизии имён
    assert s.greet_count("Анна П.") == 3
    s.forget_person("Анна П.")                       # удаление человека чистит счёт
    assert s.greets_by_person() == {}
    assert s.counters()["greets_sent"] == 3          # общая история не стирается


def test_server_init_makes_dirs_absolute(tmp_path, monkeypatch, fake_fr):
    """Все рабочие каталоги сервера становятся абсолютными (фикс 404/500)."""
    import os
    from facegate.server import RecognitionServer, parse_args

    monkeypatch.chdir(tmp_path)
    args = parse_args([
        "--video-port", "0", "--command-port", "0", "--web-port", "0",
        "--known-faces-dir", "known_faces",
        "--voice-cache-dir", "voice_cache",
        "--soundboard-dir", "soundboard",
        "--debug-dir", "debug_frames",
        "--admin-data-dir", "admin_data",
    ])
    srv = RecognitionServer(args)
    try:
        for attr in ("known_faces_dir", "voice_cache_dir", "soundboard_dir",
                     "debug_dir", "admin_data_dir"):
            assert os.path.isabs(getattr(srv.args, attr)), attr
        assert os.path.isabs(srv.store.faces_dir)
        assert os.path.isabs(srv.voice.cache_dir)
        assert os.path.isabs(srv.soundboard.sounds_dir)
    finally:
        srv.voice.stop(timeout=0.5)
