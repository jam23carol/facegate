# -*- coding: utf-8 -*-
"""Админ-панель: авторизация, камеры/трансляция, настройки, журнал, объявления."""
import io
import json
import logging
import re
import time

import cv2
import numpy as np
import pytest
import zmq

from facegate.web.auth import AdminAuth
from facegate.commands import CommandSender
from facegate.frames import FrameHub
from tests.conftest import DummyVoice, make_frame, make_wav_bytes
from facegate.web.app import create_app


def png_upload(name, filename="photo.png", count=1):
    ok, buf = cv2.imencode(".png", np.zeros((40, 40, 3), dtype=np.uint8))
    assert ok
    files = [(io.BytesIO(buf.tobytes()), filename if i == 0 else f"p{i}.png")
             for i in range(count)]
    return {"name": name, "photos": files}


@pytest.fixture
def panel(full_app_factory, tmp_path):
    """Панель с настоящей авторизацией и включённым CSRF."""
    auth = AdminAuth(data_dir=str(tmp_path / "admin"), username="admin", password="pass123")
    app, deps = full_app_factory(protect=True, auth=auth)
    app.config["CSRF_ENABLED"] = True
    client = app.test_client()
    return client, deps, auth, app


def csrf_from(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html) or \
        re.search(r'id="csrf" name="csrf-token" content="([^"]+)"', html)
    assert m, "CSRF-токен не найден в странице"
    return m.group(1)


def login(client, username="admin", password="pass123"):
    page = client.get("/login")
    token = csrf_from(page.get_data(as_text=True))
    return client.post("/login", data={"username": username, "password": password,
                                       "csrf_token": token, "next": "/"},
                       follow_redirects=False)


# ---------- доступ ----------
def test_index_requires_login(panel):
    client, *_ = panel
    res = client.get("/")
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/login?next=/")
    assert client.get("/api/persons").status_code == 401


def test_login_page_is_public(panel):
    client, *_ = panel
    res = client.get("/login")
    assert res.status_code == 200
    assert "Админ-панель".encode() in res.data


def test_login_wrong_password(panel):
    client, _deps, _auth, _app = panel
    res = login(client, password="nope")
    assert res.status_code == 401
    assert "Неверный пользователь".encode() in res.data


def test_login_and_access(panel):
    client, _deps, _auth, _app = panel
    res = login(client)
    assert res.status_code == 302 and res.headers["Location"].endswith("/")
    page = client.get("/")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Распознавание лиц" in body
    assert "Видео с камер в реальном времени" in body
    assert client.get("/api/persons").status_code == 200


def test_logout(panel):
    client, *_rest = panel
    login(client)
    token = csrf_from(client.get("/").get_data(as_text=True))
    res = client.post("/logout", data={"csrf_token": token})
    assert res.status_code == 302
    assert client.get("/api/persons").status_code == 401


def test_csrf_required_for_writes(panel):
    client, deps, _auth, _app = panel
    login(client)
    res = client.post("/api/settings", json={"settings": {"threshold": 0.5}})
    assert res.status_code == 403
    token = csrf_from(client.get("/").get_data(as_text=True))
    res = client.post("/api/settings", json={"settings": {"threshold": 0.5}},
                      headers={"X-CSRF-Token": token})
    assert res.status_code == 200
    assert deps["settings"].threshold == 0.5


def test_health_is_public(panel):
    client, *_ = panel
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json["status"] == "ok"


def test_password_change_via_api(panel):
    client, _deps, auth, _app = panel
    login(client)
    token = csrf_from(client.get("/").get_data(as_text=True))
    res = client.post("/api/auth/password",
                      json={"old_password": "wrong", "new_password": "newpass1"},
                      headers={"X-CSRF-Token": token})
    assert res.status_code == 400
    res = client.post("/api/auth/password",
                      json={"old_password": "pass123", "new_password": "newpass1"},
                      headers={"X-CSRF-Token": token})
    assert res.status_code == 200 and res.json["ok"] is True
    assert auth.verify("admin", "newpass1") is True


def test_default_password_warning(full_app_factory, tmp_path):
    auth = AdminAuth(data_dir=str(tmp_path / "admin2"))     # admin/admin
    app, _deps = full_app_factory(protect=True, auth=auth)
    client = app.test_client()
    page = client.get("/login")
    token = csrf_from(page.get_data(as_text=True))
    client.post("/login", data={"username": "admin", "password": "admin",
                                "csrf_token": token})
    body = client.get("/").get_data(as_text=True)
    assert "warnbar" in body
    boot = json.loads(re.search(r'<script id="boot" type="application/json">(.*?)</script>',
                                body, re.S).group(1))
    assert boot["must_change_password"] is True


# ---------- камеры и трансляция ----------
def test_cameras_api_lists_clients(full_app):
    client, deps = full_app
    deps["hub"].publish_raw("cam-a", make_frame(80, 60), meta={"hostname": "host-a"})
    res = client.get("/api/cameras")
    assert res.status_code == 200
    cams = res.json["cameras"]
    assert len(cams) == 1
    assert cams[0]["client_id"] == "cam-a"
    assert cams[0]["online"] is True
    assert cams[0]["width"] == 80 and cams[0]["height"] == 60
    assert cams[0]["hostname"] == "host-a"
    assert res.json["totals"]["clients_online"] == 1


def test_single_camera_and_404(full_app):
    client, deps = full_app
    deps["hub"].publish_raw("cam-a", make_frame())
    assert client.get("/api/cameras/cam-a").status_code == 200
    assert client.get("/api/cameras/ghost").status_code == 404


def test_forget_camera(full_app):
    client, deps = full_app
    deps["hub"].publish_raw("cam-a", make_frame())
    assert client.delete("/api/cameras/cam-a").json["ok"] is True
    assert deps["hub"].clients() == []


def test_stream_endpoint_is_mjpeg(full_app):
    client, deps = full_app
    hub = deps["hub"]
    hub.publish_raw("cam-a", make_frame(64, 48))
    # buffered=False: поток бесконечный, читаем только первый чанк
    res = client.get("/stream/cam-a", buffered=False)
    try:
        assert res.status_code == 200
        assert res.mimetype == "multipart/x-mixed-replace"
        assert "boundary=faceframe" in res.headers["Content-Type"]
        chunk = next(res.iter_encoded())
        assert chunk.startswith(b"--faceframe\r\n")
        assert b"Content-Type: image/jpeg" in chunk
    finally:
        res.close()


def test_camera_frame_jpg(full_app):
    client, deps = full_app
    deps["hub"].publish_raw("cam-a", make_frame(48, 32))
    res = client.get("/api/cameras/cam-a/frame.jpg")
    assert res.status_code == 200
    assert res.mimetype == "image/jpeg"
    img = cv2.imdecode(np.frombuffer(res.data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None and img.shape[:2] == (32, 48)
    assert client.get("/api/cameras/ghost/frame.jpg").status_code == 404


def test_snapshot_saved_and_served(full_app):
    client, deps = full_app
    hub = deps["hub"]
    hub.publish_raw("cam-a", make_frame())
    hub.publish_annotation_result("cam-a", make_frame(color=(0, 255, 0)), [])
    res = client.post("/api/cameras/cam-a/snapshot")
    assert res.status_code == 200
    name = res.json["name"]
    assert name.endswith(".jpg")
    assert deps["stats"].counters()["snapshots_saved"] == 1
    got = client.get(f"/snapshots/{name}")
    assert got.status_code == 200 and got.mimetype == "image/jpeg"
    listing = client.get("/api/snapshots").json
    assert any(s["name"] == name for s in listing["snapshots"])


def test_snapshot_without_frames_404(full_app):
    client, _deps = full_app
    assert client.post("/api/cameras/ghost/snapshot").status_code == 404


def test_snapshot_path_traversal_blocked(full_app):
    client, _deps = full_app
    assert client.get("/snapshots/..%2fserver.py").status_code == 404
    assert client.get("/snapshots/notes.txt").status_code == 404


def test_camera_page(full_app):
    client, deps = full_app
    deps["hub"].publish_raw("cam-a", make_frame())
    res = client.get("/camera/cam-a")
    assert res.status_code == 200
    assert "/stream/cam-a" in res.get_data(as_text=True)


# ---------- статус / журнал ----------
def test_status_payload(full_app):
    client, deps = full_app
    deps["stats"].incr("frames_received", 7)
    deps["hub"].publish_raw("cam-a", make_frame())
    res = client.get("/api/status")
    assert res.status_code == 200
    body = res.json
    assert body["counters"]["frames_received"] == 7
    assert body["clients_online"] == 1
    assert body["settings"]["threshold"] == 0.6
    assert "uptime_human" in body and body["version"]


def test_logs_and_events_incremental(full_app):
    client, deps = full_app
    logger = logging.getLogger("test.panel.logs")
    logger.handlers = [deps["logs"]]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.info("сообщение %s", "привет")
    deps["events"].push("greet", name="Анна", text="Здравствуйте, Анна!",
                        client_id="cam-a", audio=True)
    res = client.get("/api/logs?since=0").json
    assert res["lines"][0]["message"] == "сообщение привет"
    assert res["next"] == 1
    assert client.get("/api/logs?since=1").json["lines"] == []

    ev = client.get("/api/events?since=0").json
    assert ev["events"][0]["name"] == "Анна"
    assert ev["next"] == 1
    assert client.get("/api/events?since=1").json["events"] == []


# ---------- настройки ----------
def test_settings_get_describes_spec(full_app):
    client, _deps = full_app
    res = client.get("/api/settings")
    assert res.status_code == 200
    assert res.json["settings"]["threshold"] == 0.6
    assert "threshold" in res.json["spec"]
    assert res.json["groups"]["recognition"]


def test_settings_update_applies_to_hub_and_throttle(full_app):
    client, deps = full_app
    res = client.post("/api/settings", json={"settings": {
        "threshold": 0.45, "stream_fps": 5, "jpeg_quality": 55,
        "greet_cooldown": 12, "stale_after": 20}})
    assert res.status_code == 200
    assert res.json["applied"]["threshold"] == 0.45
    assert deps["hub"].stream_fps == 5.0
    assert deps["hub"].jpeg_quality == 55
    assert deps["hub"].stale_after == 20.0
    assert deps["throttle"].cooldown == 12.0


def test_settings_update_rebuilds_voice(full_app):
    client, deps = full_app
    deps["store"].add_photo("Анна", _png_bytes())
    res = client.post("/api/settings", json={"settings": {"greet_template": "Привет, {name}!"}})
    assert res.status_code == 200
    assert res.json["voice_rebuild"] == 1
    assert deps["voice"].template == "Привет, {name}!"


def test_settings_invalid_values(full_app):
    client, _deps = full_app
    res = client.post("/api/settings", json={"settings": {"threshold": 9, "detect_model": "x"}})
    assert res.status_code == 422
    assert "threshold" in res.json["errors"] and "detect_model" in res.json["errors"]
    assert client.post("/api/settings", json={}).status_code == 400


def test_throttle_reset(full_app):
    client, deps = full_app
    deps["throttle"].allow("Анна")
    assert deps["throttle"].wait_left("Анна") > 0
    assert client.post("/api/throttle/reset").json["ok"] is True
    assert deps["throttle"].snapshot() == {}


# ---------- люди (расширенные операции) ----------
def _png_bytes():
    ok, buf = cv2.imencode(".png", np.zeros((40, 40, 3), dtype=np.uint8))
    assert ok
    return buf.tobytes()


def test_persons_include_phrase(full_app):
    client, deps = full_app
    client.post("/api/persons", data=png_upload("Анна"), content_type="multipart/form-data")
    persons = client.get("/api/persons").json["persons"]
    assert persons[0]["phrase"] == "Здравствуйте, Анна!"
    assert persons[0]["voice_status"] == "ready"


def test_persons_include_greet_count(full_app):
    """Персональная статистика приветствий видна в /api/persons и /api/status."""
    client, deps = full_app
    client.post("/api/persons", data=png_upload("Анна"), content_type="multipart/form-data")
    persons = client.get("/api/persons").json["persons"]
    assert persons[0]["greet_count"] == 0

    deps["stats"].incr_greet("Анна")
    deps["stats"].incr_greet("Анна")
    persons = client.get("/api/persons").json["persons"]
    assert persons[0]["greet_count"] == 2

    status = client.get("/api/status").json
    assert status["counters"]["greets_sent"] == 2
    assert status["counters"]["greets_by_person"] == {"Анна": 2}
    # текущие (не накопительные) лица в кадре
    assert status["current_faces"] == 0
    assert status["totals"]["current_faces"] == 0


def test_greet_count_follows_rename_and_delete(full_app):
    """Переименование переносит статистику, удаление — сбрасывает."""
    client, deps = full_app
    client.post("/api/persons", data=png_upload("Анна"), content_type="multipart/form-data")
    deps["stats"].incr_greet("Анна")

    res = client.post("/api/persons/Анна/rename", json={"name": "Анна Петровна"})
    assert res.status_code == 200
    persons = client.get("/api/persons").json["persons"]
    assert persons[0]["name"] == "Анна Петровна"
    assert persons[0]["greet_count"] == 1

    client.delete("/api/persons/Анна Петровна")
    assert deps["stats"].greets_by_person() == {}


def test_delete_single_photo(full_app):
    client, deps = full_app
    client.post("/api/persons", data=png_upload("Анна", count=2),
                content_type="multipart/form-data")
    assert len(deps["store"].photos("Анна")) == 2
    first = deps["store"].photos("Анна")[0]
    res = client.delete(f"/api/persons/Анна/photos/{first}")
    assert res.status_code == 200
    assert len(deps["store"].photos("Анна")) == 1
    # удаление последнего фото убирает человека
    last = deps["store"].photos("Анна")[0]
    client.delete(f"/api/persons/Анна/photos/{last}")
    assert deps["store"].has("Анна") is False
    assert client.delete("/api/persons/Анна/photos/x.jpg").status_code == 404


def test_rename_person(full_app):
    client, deps = full_app
    client.post("/api/persons", data=png_upload("Анна"), content_type="multipart/form-data")
    res = client.post("/api/persons/Анна/rename", json={"name": "Анна Петрова"})
    assert res.status_code == 200
    assert deps["store"].has("Анна Петрова") is True
    assert deps["store"].has("Анна") is False
    assert deps["store"].photos("Анна Петрова") == ["Анна Петрова.jpg"]
    assert client.post("/api/persons/Анна Петрова/rename", json={"name": "  "}).status_code == 400
    assert client.post("/api/persons/Нет/rename", json={"name": "Кто-то"}).status_code == 404


def test_manual_greet_sends_command(full_app_factory):
    app, deps = full_app_factory(with_sender=True)
    client = app.test_client()
    sender = deps["sender"]
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://127.0.0.1:{sender.bound_port}")
    sub.setsockopt_string(zmq.SUBSCRIBE, "cam-a")
    try:
        time.sleep(0.35)
        deps["hub"].publish_raw("cam-a", make_frame())
        client.post("/api/persons", data=png_upload("Анна"),
                    content_type="multipart/form-data")
        res = client.post("/api/persons/Анна/greet", json={"client_id": "cam-a"})
        assert res.status_code == 200
        assert res.json["client_id"] == "cam-a"
        assert sub.poll(3000)
        topic = sub.recv_string()
        msg = json.loads(sub.recv_string())
        assert topic == "cam-a"
        assert msg["action"] == "greet" and msg["name"] == "Анна"
        assert msg["audio_b64"]
    finally:
        sub.close(0)
        ctx.term()


def test_manual_greet_unknown_person(full_app):
    client, _deps = full_app
    assert client.post("/api/persons/Нет/greet", json={}).status_code == 404


def test_announce_to_client(full_app_factory):
    app, deps = full_app_factory(with_sender=True)
    client = app.test_client()
    sender = deps["sender"]
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://127.0.0.1:{sender.bound_port}")
    sub.setsockopt_string(zmq.SUBSCRIBE, "")
    try:
        time.sleep(0.35)
        deps["hub"].publish_raw("cam-a", make_frame())
        res = client.post("/api/announce", json={"text": "Обед через 5 минут"})
        assert res.status_code == 200
        assert res.json["targets"] == ["cam-a"]
        assert sub.poll(3000)
        sub.recv_string()
        first = json.loads(sub.recv_string())
        assert first["action"] == "speak" and first["text"] == "Обед через 5 минут"
        # следом приходит та же фраза с озвучкой
        if sub.poll(3000):
            sub.recv_string()
            second = json.loads(sub.recv_string())
            assert second["audio_b64"]
        assert deps["stats"].counters()["manual_announces"] >= 1
    finally:
        sub.close(0)
        ctx.term()


def test_announce_requires_text_and_clients(full_app_factory):
    app, deps = full_app_factory(with_sender=True)
    client = app.test_client()
    assert client.post("/api/announce", json={"text": ""}).status_code == 400
    # sender есть, но в эфире никого нет
    assert client.post("/api/announce", json={"text": "есть кто?"}).status_code == 409
    deps["hub"].publish_raw("cam-a", make_frame())
    assert client.post("/api/announce", json={"text": "теперь слышно"}).status_code == 200


def test_announce_without_sender(full_app):
    client, _deps = full_app
    res = client.post("/api/announce", json={"text": "текст", "client_id": "cam-a"})
    assert res.status_code == 503


# ---------- озвучка ----------
def test_voice_info(full_app):
    client, deps = full_app
    client.post("/api/persons", data=png_upload("Анна"), content_type="multipart/form-data")
    body = client.get("/api/voice-info").json
    assert body["ready"] == 1 and body["files"] >= 1
    assert body["template"] == "Здравствуйте, {name}!"
    assert body["engine"] == "dummy"


def test_voice_rebuild(full_app):
    client, deps = full_app
    client.post("/api/persons", data=png_upload("Анна"), content_type="multipart/form-data")
    res = client.post("/api/voice-info/rebuild")
    assert res.status_code == 200 and res.json["queued"] == 1


def test_voice_failure_status(full_app_factory, failing_dummy_voice):
    app, _deps = full_app_factory(voice=failing_dummy_voice)
    client = app.test_client()
    res = client.post("/api/persons", data=png_upload("Кэрол"),
                      content_type="multipart/form-data")
    assert res.json["voice"] == "error"
    info = client.get("/api/voice-info").json
    assert info["errors"] == 1 and info["error_list"] == ["Кэрол"]


# ---------- прочее ----------
def test_photo_serving(full_app):
    client, deps = full_app
    client.post("/api/persons", data=png_upload("Анна"), content_type="multipart/form-data")
    fname = deps["store"].photos("Анна")[0]
    res = client.get(f"/photo/{fname}")
    assert res.status_code == 200 and res.mimetype.startswith("image/")
    assert client.get("/photo/readme.txt").status_code == 404
    assert client.get("/photo/..%2fserver.py").status_code == 404


def test_upload_too_large_rejected(full_app):
    client, _deps = full_app
    big = io.BytesIO(b"0" * (26 * 1024 * 1024))
    res = client.post("/api/persons",
                      data={"name": "Боб", "photos": [(big, "big.png")]},
                      content_type="multipart/form-data")
    assert res.status_code == 413


def test_auth_disabled_mode(full_app):
    client, _deps = full_app          # full_app создаётся с protect=False
    assert client.get("/").status_code == 200
    assert client.get("/api/persons").status_code == 200
    res = client.post("/api/auth/password", json={"old_password": "a", "new_password": "bbbb"})
    assert res.status_code == 400


def test_users_api(full_app_factory, tmp_path):
    auth = AdminAuth(data_dir=str(tmp_path / "admin3"), username="admin", password="pass123")
    app, _deps = full_app_factory(protect=True, auth=auth)
    client = app.test_client()
    login(client)
    users = client.get("/api/auth/users").json["users"]
    assert [u["username"] for u in users] == ["admin"]
    res = client.post("/api/auth/users", json={"username": "second", "password": "secret1"})
    assert res.status_code == 200
    assert client.delete("/api/auth/users/second").json["ok"] is True
    assert client.delete("/api/auth/users/admin").status_code == 400


# ---------------------------------------------------------------------------
# Регрессии озвучки объявлений: один пакет, всегда со звуком, кэш не повторно
# ---------------------------------------------------------------------------
class Subscriber:
    """SUB-сокет для проверки команд, уходящих клиентам."""

    def __init__(self, port, topic=""):
        self.ctx = zmq.Context()
        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.connect(f"tcp://127.0.0.1:{port}")
        self.sub.setsockopt_string(zmq.SUBSCRIBE, topic)
        self.sub.setsockopt(zmq.RCVTIMEO, 200)
        time.sleep(0.35)                    # PUB/SUB slow joiner

    def recv_all(self, wait=1.5):
        """Все пакеты, которые пришли за ``wait`` секунд."""
        out = []
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                topic = self.sub.recv_string()
                out.append((topic, json.loads(self.sub.recv_string())))
            except zmq.Again:
                continue
        return out

    def close(self):
        self.sub.close(0)
        self.ctx.term()


def test_announce_single_command_with_audio_and_cache_reuse(full_app_factory):
    """Одно объявление = ОДНА команда, и всегда со звуком.

    Раньше панель сначала слала speak с audio_b64=null (клиент молчал), затем
    вторую команду из фонового потока — и синтезировала фразу заново при каждом
    нажатии, даже если WAV уже лежал в кэше.
    """
    app, deps = full_app_factory(with_sender=True)
    client = app.test_client()
    sub = Subscriber(deps["sender"].bound_port)
    try:
        deps["hub"].publish_raw("cam-a", make_frame())
        text = "Обед через пять минут"

        res = client.post("/api/announce", json={"text": text})
        assert res.status_code == 200
        assert res.json["voice"] == "ready" and res.json["cached"] is False
        packets = sub.recv_all()
        assert len(packets) == 1, f"ожидалась одна команда, пришло {len(packets)}"
        assert packets[0][1]["action"] == "speak"
        assert packets[0][1]["text"] == text
        assert packets[0][1]["audio_b64"], "команда ушла без аудио"

        # повтор того же текста — озвучка берётся из кэша, синтез не запускается
        synth_before = len(deps["voice"].announced)
        res2 = client.post("/api/announce", json={"text": text})
        assert res2.json["voice"] == "ready" and res2.json["cached"] is True
        packets2 = sub.recv_all()
        assert len(packets2) == 1 and packets2[0][1]["audio_b64"]
        assert len(deps["voice"].announced) == synth_before, \
            "готовая фраза синтезирована повторно"
    finally:
        sub.close()


def test_announce_pending_path_never_sends_empty_packet(full_app_factory):
    """Если озвучка ещё не готова — команда приходит из фона, но тоже со звуком."""
    app, deps = full_app_factory(with_sender=True)
    deps["settings"].update({"announce_wait": 0.0})      # не ждать синхронно
    client = app.test_client()
    sub = Subscriber(deps["sender"].bound_port)
    try:
        deps["hub"].publish_raw("cam-a", make_frame())
        res = client.post("/api/announce", json={"text": "Проверка фоновой озвучки"})
        assert res.status_code == 200
        assert res.json["voice"] == "pending" and res.json["sent"] == 0
        packets = sub.recv_all(wait=3.0)
        assert len(packets) == 1, f"ожидался один пакет, пришло {len(packets)}"
        assert packets[0][1]["audio_b64"], "клиент получил команду без аудио"
        assert deps["stats"].counters()["manual_announces"] >= 1
    finally:
        sub.close()


def test_announce_without_tts_reports_unavailable(full_app_factory, failing_dummy_voice):
    """TTS недоступен вовсе — отправляется текст и панель честно об этом пишет."""
    app, deps = full_app_factory(with_sender=True, voice=failing_dummy_voice)
    client = app.test_client()
    deps["settings"].update({"announce_wait": 0.0})
    sub = Subscriber(deps["sender"].bound_port)
    try:
        deps["hub"].publish_raw("cam-a", make_frame())
        res = client.post("/api/announce", json={"text": "Только текст"})
        assert res.status_code == 200
        assert res.json["voice"] == "unavailable"
        assert "TTS" in res.json["warning"]
        packets = sub.recv_all()
        assert len(packets) == 1 and packets[0][1]["audio_b64"] is None
    finally:
        sub.close()


def test_announce_uses_soft_online_window(full_app_factory):
    """Короткая потеря видеокадров больше не даёт 409 «Нет клиентов в эфире»."""
    app, deps = full_app_factory(with_sender=True)
    client = app.test_client()
    deps["hub"].stale_after = 0.05           # configure() зажимает порог снизу 1 с
    deps["hub"].command_stale_after = 30.0
    deps["hub"].publish_raw("cam-a", make_frame())
    time.sleep(0.1)
    assert deps["hub"].online_clients() == []            # в панели «нет сигнала»
    res = client.post("/api/announce", json={"text": "Слышно?"})
    assert res.status_code == 200, res.json
    assert res.json["targets"] == ["cam-a"]


def test_soundboard_broadcast_uses_soft_online_window(full_app_factory):
    app, deps = full_app_factory(with_sender=True, with_soundboard=True)
    client = app.test_client()
    deps["hub"].stale_after = 0.05           # configure() зажимает порог снизу 1 с
    deps["hub"].command_stale_after = 30.0
    deps["hub"].publish_raw("cam-a", make_frame())
    ok, item = deps["soundboard"].add_upload("gong.wav", make_wav_bytes(), title="Гонг")
    assert ok
    time.sleep(0.1)
    res = client.post(f"/api/soundboard/{item['id']}/broadcast", json={})
    assert res.status_code == 200, res.json
    assert res.json["targets"] == ["cam-a"]


class LateVoice(DummyVoice):
    """Озвучка появляется не сразу — как при холодном старте TTS-движка."""

    def __init__(self, ready_after=2, **kwargs):
        super().__init__(**kwargs)
        self.ready_after = ready_after
        self.polls = 0

    def get_random(self, name):
        self.polls += 1
        if self.polls <= self.ready_after:
            return self.text_for(name), None
        return super().get_random(name)


def test_manual_greet_waits_for_audio(full_app_factory):
    """«Проверить привет» дожидается озвучки: клиент не получает пустую команду."""
    voice = LateVoice(ready_after=2)
    app, deps = full_app_factory(with_sender=True, voice=voice)
    client = app.test_client()
    sub = Subscriber(deps["sender"].bound_port, topic="cam-a")
    try:
        deps["hub"].publish_raw("cam-a", make_frame())
        client.post("/api/persons", data=png_upload("Анна"),
                    content_type="multipart/form-data")
        res = client.post("/api/persons/Анна/greet", json={"client_id": "cam-a"})
        assert res.status_code == 200, res.json
        assert res.json["voice"] == "ready"
        assert res.json["text"]
        packets = sub.recv_all()
        assert len(packets) == 1
        assert packets[0][1]["action"] == "greet"
        assert packets[0][1]["audio_b64"], "приветствие ушло без аудио"
        assert packets[0][1]["text"] == res.json["text"]
        assert voice.polls >= 2
    finally:
        sub.close()


def test_manual_greet_uses_phrase_from_pool(full_app_factory):
    """В команду попадает именно та фраза, которая прозвучит (пул шаблонов)."""
    app, deps = full_app_factory(with_sender=True)
    client = app.test_client()
    deps["settings"].update({"greet_templates": "Добрый день, {name}!\nПривет, {name}!"})
    deps["voice"].configure(templates=["Добрый день, {name}!", "Привет, {name}!"])
    sub = Subscriber(deps["sender"].bound_port, topic="cam-a")
    try:
        deps["hub"].publish_raw("cam-a", make_frame())
        client.post("/api/persons", data=png_upload("Анна"),
                    content_type="multipart/form-data")
        seen = set()
        for _ in range(6):
            res = client.post("/api/persons/Анна/greet", json={"client_id": "cam-a"})
            assert res.status_code == 200
            seen.add(res.json["text"])
        assert seen <= {"Добрый день, Анна!", "Привет, Анна!"}
        assert len(seen) == 2, "фразы из пула должны чередоваться"
        packets = sub.recv_all(wait=1.0)
        assert packets
        assert {p[1]["text"] for p in packets} == seen
        assert all(p[1]["audio_b64"] for p in packets)
    finally:
        sub.close()


def test_settings_templates_update_voice_and_rebuild(full_app):
    client, deps = full_app
    deps["store"].add_photo("Анна", _png_bytes())
    res = client.post("/api/settings", json={"settings": {
        "greet_templates": "Здравствуйте, {name}!\nПривет, {name}!"}})
    assert res.status_code == 200, res.json
    assert res.json["voice_rebuild"] == 1
    assert deps["voice"].templates == ["Здравствуйте, {name}!", "Привет, {name}!"]
    assert deps["voice"].template == "Здравствуйте, {name}!"
    assert deps["settings"].greet_template == "Здравствуйте, {name}!"
    # оба варианта синтезируются
    info = client.get("/api/persons").json["persons"][0]
    assert info["voice_variants"] == 2
    assert len(info["phrases"]) == 2


def test_persons_list_reports_variant_progress(full_app):
    client, deps = full_app
    client.post("/api/persons", data=png_upload("Иван"),
                content_type="multipart/form-data")
    person = client.get("/api/persons").json["persons"][0]
    assert person["voice_variants"] == len(deps["voice"].templates)
    assert person["voice_ready_variants"] == person["voice_variants"]
    assert person["voice_status"] == "ready"
    assert person["phrase"] == person["phrases"][0]


def test_voice_info_reports_template_pool(full_app):
    client, _deps = full_app
    body = client.get("/api/voice-info").json
    assert body["variants"] == len(body["templates"]) >= 1
    assert body["template"] == body["templates"][0]


def test_ui_has_download_attribute_and_announce_dialog():
    """Правки интерфейса: скачивание кадра не уводит из панели, объявление — модалка."""
    from facegate.web import ui

    assert 'data-act="dl" download' in ui.JS
    assert "frame.jpg?download=1" in ui.JS
    assert "setAttribute('download'" in ui.JS
    assert 'id="announce-dialog"' in ui.ADMIN_PAGE
    assert "function submitAnnounce" in ui.JS
    # многострочное поле пула фраз приветствия
    assert "spec.type === 'text'" in ui.JS
    assert "textarea id=\"'+id+'\"" in ui.JS
    # скрытые (устаревшие) настройки не показываются в форме
    assert ".hidden" in ui.JS
    # prompt() для объявления больше не используется
    assert "prompt('Текст объявления" not in ui.JS
