# -*- coding: utf-8 -*-
"""Регрессия: относительные рабочие каталоги не должны ломать отдачу файлов.

Flask разрешает относительные пути в ``send_file`` / ``send_from_directory``
от каталога модуля приложения (``facegate/web/``), а не от cwd процесса.
Раньше из-за этого:

  * ``GET /api/soundboard/synth:…/file`` падал в **500 FileNotFoundError**
    (звук лежал в ``voice_cache/…`` относительно корня проекта);
  * ``GET /photo/<имя>.jpg`` отдавал **404**, хотя ``os.path.exists`` от cwd
    проходил успешно (``known_faces/…``).

Тесты ниже поднимают панель с полностью относительными каталогами (как в
реальном запуске ``python server.py`` из корня проекта) и проверяют, что
фото, снимки и звуки саундборда отдаются корректно.
"""
import io
import os

import cv2
import numpy as np
import pytest

from facegate.recognition.store import FaceStore
from facegate.soundboard import Soundboard
from facegate.stats import Stats
from facegate.web.app import create_app
from tests.conftest import DummyVoice, make_wav_bytes


def png_bytes():
    ok, buf = cv2.imencode(".png", np.zeros((40, 40, 3), dtype=np.uint8))
    assert ok
    return buf.tobytes()


@pytest.fixture
def rel_panel(fake_fr, tmp_path, monkeypatch):
    """Панель, у которой все рабочие каталоги заданы относительными путями.

    cwd == tmp_path (имитация запуска сервера из корня проекта).
    """
    monkeypatch.chdir(tmp_path)
    faces_dir = "known_faces"
    store = FaceStore(faces_dir)
    voice = DummyVoice()
    soundboard = Soundboard(sounds_dir="soundboard",
                            state_path=os.path.join("admin_data", "soundboard.json"))
    app = create_app(store, voice, faces_dir, protect=False,
                     debug_dir="debug_frames", soundboard=soundboard,
                     stats=Stats())
    app.config["CSRF_ENABLED"] = False
    app.config["TESTING"] = True
    yield app.test_client(), store, soundboard, voice
    voice.cleanup()


def test_photo_served_with_relative_faces_dir(rel_panel):
    """/photo/<имя>.jpg работает, даже если known_faces — относительный путь."""
    client, store, _, _ = rel_panel
    ok, message = store.add_photo("Марк", png_bytes())
    assert ok, message
    assert os.path.exists(os.path.join("known_faces", "Марк.jpg"))

    res = client.get("/photo/Марк.jpg")
    assert res.status_code == 200
    assert res.data[:2] == b"\xff\xd8" or len(res.data) > 0

    res_missing = client.get("/photo/Нету.jpg")
    assert res_missing.status_code == 404


def test_soundboard_file_served_with_relative_dirs(rel_panel):
    """Звук саундборда отдаётся по относительным каталогам (был 500)."""
    client, _, soundboard, _ = rel_panel
    wav = make_wav_bytes()
    res = client.post("/api/soundboard/upload",
                      data={"file": (io.BytesIO(wav), "test.wav"), "title": "Тест"},
                      content_type="multipart/form-data")
    assert res.status_code == 200 and res.json["ok"] is True

    items = client.get("/api/soundboard").json["items"]
    assert items, "загруженный звук не появился в списке"
    item_id = items[0]["id"]

    # resolve() обязан возвращать АБСОЛЮТНЫЙ путь (защита от root_path Flask)
    resolved = soundboard.resolve(item_id)
    assert resolved is not None
    assert os.path.isabs(resolved["path"])

    res = client.get(f"/api/soundboard/{item_id}/file")
    assert res.status_code == 200
    assert res.data == wav


def test_soundboard_missing_file_is_404_not_500(rel_panel):
    """Пропавший файл — честный 404 с JSON, а не 500 FileNotFoundError."""
    client, _, _, _ = rel_panel
    res = client.get("/api/soundboard/upload:nesuschestvuet.wav/file")
    assert res.status_code == 404
    assert res.json["ok"] is False


def test_snapshot_served_with_relative_debug_dir(rel_panel, tmp_path):
    """/snapshots/<file> работает с относительным debug_frames."""
    client, _, _, _ = rel_panel
    os.makedirs("debug_frames", exist_ok=True)
    ok, buf = cv2.imencode(".jpg", np.zeros((24, 32, 3), dtype=np.uint8))
    assert ok
    with open(os.path.join("debug_frames", "shot.jpg"), "wb") as f:
        f.write(buf.tobytes())

    res = client.get("/snapshots/shot.jpg")
    assert res.status_code == 200
    assert res.data == buf.tobytes()

    assert client.get("/snapshots/missing.jpg").status_code == 404


def test_voice_cache_and_soundboard_dirs_are_absolute(tmp_path, monkeypatch):
    """Конструкторы сразу приводят каталоги к абсолютным путям."""
    from facegate.soundboard import Soundboard as Sb
    from facegate.tts.cache import VoiceCache

    monkeypatch.chdir(tmp_path)
    sb = Sb(sounds_dir="soundboard")
    assert os.path.isabs(sb.sounds_dir)

    vc = VoiceCache(cache_dir="voice_cache")
    try:
        assert os.path.isabs(vc.cache_dir)
        assert os.path.isabs(vc.base_dir)
        assert vc.cache_dir == os.path.join(str(tmp_path), "voice_cache", "greetings")
    finally:
        vc.stop(timeout=1.0)
