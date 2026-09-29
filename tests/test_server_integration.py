# -*- coding: utf-8 -*-
"""Сквозная проверка сервера: ZMQ-кадры → распознавание → приветствие → веб-панель.

Сервер поднимается по-настоящему (все потоки, реальные сокеты и HTTP), но:
  * ``face_recognition`` подменён заглушкой — dlib не требуется;
  * ``VoiceCache`` подменён на ``DummyVoice`` — TTS не требуется;
  * GUI запрещён: ``cv2.imshow`` и компания бросают исключение, а в конце
    проверяется, что сервер их ни разу не вызвал (на сервере нет Qt-окна —
    всё видео уходит в админ-панель).
"""
import json
import socket
import time
import urllib.request

import cv2
import numpy as np
import pytest
import zmq

import facegate.server as server_mod
from facegate.recognition.store import FaceStore
from facegate.server import RecognitionServer, parse_args
from tests.conftest import DummyVoice


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def png_bytes(w=48, h=40):
    ok, buf = cv2.imencode(".png", np.zeros((h, w, 3), dtype=np.uint8))
    assert ok
    return buf.tobytes()


def jpeg_frame(w=96, h=72, color=(40, 80, 120)):
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:, :] = color
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    return buf.tobytes()


@pytest.fixture
def gui_guard(monkeypatch):
    """Любая попытка открыть окно OpenCV на сервере — провал теста."""
    calls = []

    def boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("сервер не должен открывать окна OpenCV/Qt")

    for name in ("imshow", "waitKey", "waitKeyEx", "startWindowThread",
                 "namedWindow", "destroyAllWindows"):
        monkeypatch.setattr(cv2, name, boom, raising=False)
    return calls


@pytest.fixture
def server_factory(tmp_path, fake_fr, monkeypatch, gui_guard):
    made = []

    def make(extra_args=(), persons=("Анна",), voice_fail=False):
        monkeypatch.setattr(
            server_mod, "VoiceCache",
            lambda **kw: DummyVoice(template=kw.get("template"),
                                    templates=kw.get("templates"),
                                    fail=voice_fail))
        faces_dir = tmp_path / "faces"
        faces_dir.mkdir(exist_ok=True)
        store = FaceStore(str(faces_dir))
        for name in persons:
            ok, _msg = store.add_photo(name, png_bytes())
            assert ok, "не удалось подготовить эталон для тестов"
        args = parse_args([
            "--video-port", str(free_port()),
            "--command-port", str(free_port()),
            "--web-port", str(free_port()),
            "--known-faces-dir", str(faces_dir),
            "--voice-cache-dir", str(tmp_path / "voice_cache"),
            "--admin-data-dir", str(tmp_path / "admin_data"),
            "--debug-dir", str(tmp_path / "debug_frames"),
            "--no-auth",
            "--greet-cooldown", "0.05",
            "--log-level", "ERROR",
            "--stats-interval", "0",
            *extra_args,
        ])
        srv = RecognitionServer(args)
        srv.start()
        made.append(srv)
        return srv

    yield make
    for srv in made:
        srv.shutdown()


@pytest.fixture
def zmq_ctx():
    ctx = zmq.Context()
    yield ctx
    ctx.term()


def subscribe(ctx, port, topic, wait=0.5):
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.LINGER, 0)      # не блокировать ctx.term() при падении теста
    sub.connect(f"tcp://127.0.0.1:{port}")
    sub.setsockopt_string(zmq.SUBSCRIBE, topic)
    sub.setsockopt(zmq.RCVTIMEO, 100)
    time.sleep(wait)            # PUB/SUB slow joiner
    return sub


def push_socket(ctx, port):
    push = ctx.socket(zmq.PUSH)
    push.setsockopt(zmq.LINGER, 0)
    push.connect(f"tcp://127.0.0.1:{port}")
    return push


def pool_texts(name, settings):
    """Все варианты фразы приветствия из пула настроек."""
    return [t.replace("{name}", name) for t in settings.greet_template_list]


def send_frame(push, client_id, jpg, meta=None):
    push.send_string(client_id, flags=zmq.SNDMORE)
    if meta is not None:
        push.send(jpg, flags=zmq.SNDMORE)
        push.send_string(json.dumps(meta, ensure_ascii=False))
    else:
        push.send(jpg)


def wait_for(predicate, timeout=8.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def recv_command(sub, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            topic = sub.recv_string()
            return topic, json.loads(sub.recv_string())
        except zmq.Again:
            continue
    return None, None


# ---------- тесты ----------
def test_full_cycle_greet_and_stream(server_factory, zmq_ctx, gui_guard):
    srv = server_factory()
    client_id = "cam-test"
    sub = subscribe(zmq_ctx, srv.args.command_port, client_id)
    push = push_socket(zmq_ctx, srv.args.video_port)

    for _ in range(3):
        send_frame(push, client_id, jpeg_frame())
        time.sleep(0.05)

    # 1) команда приветствия пришла клиенту
    topic, msg = recv_command(sub)
    assert topic == client_id
    assert msg["action"] == "greet"
    assert msg["name"] == "Анна"
    # фраза выбирается случайно из пула шаблонов (по умолчанию их три)
    assert msg["text"] in pool_texts("Анна", srv.settings)
    assert msg["audio_b64"], "приветствие должно прийти с озвучкой"

    # 2) кадр виден в админ-панели (вместо окна OpenCV)
    info = wait_for(lambda: srv.hub.client_info(client_id))
    assert info and info["frames"] >= 1
    assert info["width"] == 96 and info["height"] == 72
    raw, seq = srv.hub.latest_raw(client_id)
    assert raw and seq >= 1
    img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None and img.shape[:2] == (72, 96)

    # 3) статистика и события
    counters = srv.stats.counters()
    assert counters["frames_received"] >= 1
    assert counters["greets_sent"] >= 1
    assert counters["faces_detected"] >= 1
    kinds = [e["type"] for e in srv.events.recent(20)]
    assert "greet" in kinds

    # 4) веб-панель реально отвечает по HTTP
    url = f"http://127.0.0.1:{srv.args.web_port}/api/health"
    body = wait_for(lambda: _get_json(url))
    assert body and body["status"] == "ok"
    assert body["clients_online"] >= 1

    # 5) никаких окон OpenCV на сервере
    assert gui_guard == []

    push.close(0)
    sub.close(0)


def _get_json(url, timeout=2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def test_metadata_from_client_visible_in_panel(server_factory, zmq_ctx):
    srv = server_factory()
    push = push_socket(zmq_ctx, srv.args.video_port)
    meta = {"client_id": "cam-meta", "hostname": "raspberry", "camera_id": 1,
            "fps": 10, "width": 96, "height": 72, "version": "2.0.0"}
    send_frame(push, "cam-meta", jpeg_frame(), meta=meta)
    info = wait_for(lambda: (srv.hub.client_info("cam-meta") or {}).get("hostname"))
    assert info == "raspberry"
    assert srv.hub.client_info("cam-meta")["camera_id"] == 1
    push.close(0)


def test_greet_respects_cooldown(server_factory, zmq_ctx):
    srv = server_factory(extra_args=["--greet-cooldown", "5"])
    sub = subscribe(zmq_ctx, srv.args.command_port, "cam-cd")
    push = push_socket(zmq_ctx, srv.args.video_port)
    for _ in range(5):
        send_frame(push, "cam-cd", jpeg_frame())
        time.sleep(0.05)
    _topic, msg = recv_command(sub, timeout=6.0)
    assert msg and msg["action"] == "greet"
    # следующие кадры в пределах кулдауна — новых приветствий нет
    for _ in range(5):
        send_frame(push, "cam-cd", jpeg_frame())
        time.sleep(0.05)
    wait_for(lambda: srv.stats.counters()["greets_throttled"] >= 1)
    assert srv.stats.counters()["greets_sent"] == 1
    assert srv.stats.counters()["greets_throttled"] >= 1
    topic2, _msg2 = recv_command(sub, timeout=0.6)
    assert topic2 is None
    push.close(0)
    sub.close(0)


def test_runtime_toggle_recognition_and_greeting(server_factory, zmq_ctx):
    srv = server_factory()
    sub = subscribe(zmq_ctx, srv.args.command_port, "cam-t")
    push = push_socket(zmq_ctx, srv.args.video_port)

    # выключаем отправку приветствий на лету (как кнопкой в панели)
    srv.settings.update({"greeting_enabled": False})
    for _ in range(3):
        send_frame(push, "cam-t", jpeg_frame())
        time.sleep(0.05)
    wait_for(lambda: srv.stats.counters()["frames_processed"] >= 1)
    assert srv.stats.counters()["greets_sent"] == 0
    assert srv.hub.client_info("cam-t")["faces"] >= 1     # лица при этом найдены

    # включаем обратно
    srv.settings.update({"greeting_enabled": True})
    for _ in range(3):
        send_frame(push, "cam-t", jpeg_frame())
        time.sleep(0.05)
    _topic, msg = recv_command(sub, timeout=6.0)
    assert msg and msg["action"] == "greet"

    # полное отключение распознавания: видео идёт, лица не ищутся
    srv.settings.update({"recognition_enabled": False, "greet_cooldown": 0})
    before_faces = srv.stats.counters()["faces_detected"]
    before_processed = srv.stats.counters()["frames_processed"]
    for _ in range(3):
        send_frame(push, "cam-t", jpeg_frame())
        time.sleep(0.05)
    wait_for(lambda: srv.stats.counters()["frames_processed"] > before_processed)
    assert srv.stats.counters()["faces_detected"] == before_faces
    assert srv.hub.client_info("cam-t")["has_video"] is True
    push.close(0)
    sub.close(0)


def test_unknown_face_no_greet(server_factory, zmq_ctx):
    srv = server_factory(persons=())          # база эталонов пуста
    sub = subscribe(zmq_ctx, srv.args.command_port, "cam-u")
    push = push_socket(zmq_ctx, srv.args.video_port)
    for _ in range(3):
        send_frame(push, "cam-u", jpeg_frame())
        time.sleep(0.05)
    wait_for(lambda: srv.stats.counters()["frames_processed"] >= 1)
    assert srv.stats.counters()["greets_sent"] == 0
    topic, _msg = recv_command(sub, timeout=0.6)
    assert topic is None
    push.close(0)
    sub.close(0)


def test_broken_frame_counted_not_crashing(server_factory, zmq_ctx):
    srv = server_factory()
    push = push_socket(zmq_ctx, srv.args.video_port)
    push.send_string("cam-bad", flags=zmq.SNDMORE)
    push.send(b"\xff\xd8not-a-jpeg")
    wait_for(lambda: srv.stats.counters()["decode_errors"] >= 1)
    assert srv.stats.counters()["decode_errors"] >= 1
    # сервер жив и продолжает принимать нормальные кадры
    send_frame(push, "cam-bad", jpeg_frame())
    assert wait_for(lambda: srv.hub.client_info("cam-bad")) is not None
    push.close(0)


def test_multiple_clients_isolated(server_factory, zmq_ctx):
    srv = server_factory()
    push = push_socket(zmq_ctx, srv.args.video_port)
    send_frame(push, "cam-1", jpeg_frame(color=(10, 10, 10)))
    send_frame(push, "cam-2", jpeg_frame(color=(200, 200, 200)))
    wait_for(lambda: len(srv.hub.clients()) >= 2)
    assert srv.hub.clients() == ["cam-1", "cam-2"]
    assert srv.hub.client_info("cam-1")["frames"] >= 1
    assert srv.hub.client_info("cam-2")["frames"] >= 1
    push.close(0)


def test_save_debug_writes_snapshot(server_factory, zmq_ctx, tmp_path):
    srv = server_factory(extra_args=["--save-debug"])
    push = push_socket(zmq_ctx, srv.args.video_port)
    send_frame(push, "cam-s", jpeg_frame())
    ok = wait_for(lambda: srv.stats.counters()["greets_sent"] >= 1 and
                  srv.hub.list_snapshots(srv.args.debug_dir))
    assert ok, "снимок приветствия не сохранён"
    names = [s["name"] for s in srv.hub.list_snapshots(srv.args.debug_dir)]
    assert any(n.startswith("greet_") for n in names)
    push.close(0)


def test_voice_missing_still_greets_with_text(server_factory, zmq_ctx):
    srv = server_factory(voice_fail=True)
    sub = subscribe(zmq_ctx, srv.args.command_port, "cam-v")
    push = push_socket(zmq_ctx, srv.args.video_port)
    for _ in range(3):
        send_frame(push, "cam-v", jpeg_frame())
        time.sleep(0.05)
    _topic, msg = recv_command(sub, timeout=6.0)
    assert msg and msg["action"] == "greet"
    assert msg["audio_b64"] is None
    # без озвучки берётся первая фраза пула (текст всё равно уходит клиенту)
    assert msg["text"] == pool_texts("Анна", srv.settings)[0]
    assert srv.stats.counters()["greets_no_audio"] >= 1
    push.close(0)
    sub.close(0)


def test_web_api_over_http(server_factory, zmq_ctx):
    srv = server_factory()
    push = push_socket(zmq_ctx, srv.args.video_port)
    send_frame(push, "cam-http", jpeg_frame())
    base = f"http://127.0.0.1:{srv.args.web_port}"
    assert wait_for(lambda: _get_json(base + "/api/health")) is not None
    status = _get_json(base + "/api/status")
    assert status["version"] and status["settings"]["threshold"] == 0.6
    cams = _get_json(base + "/api/cameras")["cameras"]
    assert any(c["client_id"] == "cam-http" for c in cams)
    # страница панели отдаётся (без авторизации — --no-auth)
    with urllib.request.urlopen(base + "/", timeout=3) as r:
        html = r.read().decode("utf-8")
    assert "Распознавание лиц" in html and "/stream/" in html
    push.close(0)


def test_shutdown_is_clean(server_factory, zmq_ctx):
    srv = server_factory()
    push = push_socket(zmq_ctx, srv.args.video_port)
    send_frame(push, "cam-stop", jpeg_frame())
    wait_for(lambda: srv.hub.client_info("cam-stop"))
    started = time.time()
    srv.shutdown()
    assert time.time() - started < 8.0
    assert srv.stop_event.is_set()
    assert not srv.receiver_thread.is_alive()
    assert not srv.recognition_thread.is_alive()
    assert srv.sender.ready() is True and srv.sender.pending == 0
    # повторный вызов безопасен
    srv.shutdown()
    push.close(0)


def test_no_web_flag_disables_panel(server_factory):
    srv = server_factory(extra_args=["--no-web"])
    assert srv.app is None
    assert srv.web_thread is None


# ---------------------------------------------------------------------------
# Вариативность приветствий в реальном цикле распознавания
# ---------------------------------------------------------------------------
POOL_ARG = "Раз, {name}!\nДва, {name}!\nТри, {name}!"


def test_greet_phrases_rotate_without_repeats(server_factory, zmq_ctx):
    """Пул фраз: приветствия чередуются и не повторяются подряд."""
    srv = server_factory(extra_args=["--greet-cooldown", "0",
                                     "--greet-templates", POOL_ARG])
    assert srv.settings.greet_template_list == ["Раз, {name}!", "Два, {name}!",
                                                "Три, {name}!"]
    assert srv.settings.greet_template == "Раз, {name}!"      # синхронизировано
    assert srv.voice.templates == srv.settings.greet_template_list

    client_id = "cam-rotate"
    sub = subscribe(zmq_ctx, srv.args.command_port, client_id)
    push = push_socket(zmq_ctx, srv.args.video_port)
    texts = []
    try:
        for i in range(14):
            send_frame(push, client_id, jpeg_frame(color=(i * 7 % 255, 40, 90)))
            topic, msg = recv_command(sub, timeout=4.0)
            if msg and msg.get("action") == "greet":
                assert msg["audio_b64"], "приветствие без озвучки"
                assert msg["name"] == "Анна"
                texts.append(msg["text"])
            time.sleep(0.02)
    finally:
        push.close(0)
        sub.close(0)

    assert len(texts) >= 6, f"слишком мало приветствий: {texts}"
    pool = {"Раз, Анна!", "Два, Анна!", "Три, Анна!"}
    assert set(texts) <= pool
    assert len(set(texts)) >= 2, "фразы не чередуются"
    assert all(a != b for a, b in zip(texts, texts[1:])), \
        f"фраза повторилась подряд: {texts}"
    # в журнал событий попадает именно прозвучавшая фраза
    greets = [e for e in srv.events.recent(50) if e["type"] == "greet"]
    assert greets and all(e["text"] in pool for e in greets)
    assert all(e["audio"] for e in greets)


def test_greet_uses_single_template_when_pool_has_one(server_factory, zmq_ctx):
    srv = server_factory(extra_args=["--greet-template", "Салют, {name}!"])
    assert srv.settings.greet_template_list == ["Салют, {name}!"]
    sub = subscribe(zmq_ctx, srv.args.command_port, "cam-single")
    push = push_socket(zmq_ctx, srv.args.video_port, )
    try:
        send_frame(push, "cam-single", jpeg_frame())
        _topic, msg = recv_command(sub)
        assert msg and msg["text"] == "Салют, Анна!"
    finally:
        push.close(0)
        sub.close(0)


def test_prewarm_synthesizes_all_variants(server_factory):
    """При старте сервер заранее синтезирует все варианты для всех людей."""
    srv = server_factory()
    variants = srv.voice.variants_for("Анна")
    assert len(variants) == len(srv.settings.greet_template_list)
    assert all(v["ready"] for v in variants)


def test_web_status_exposes_new_settings(server_factory):
    """Настройки пула фраз и порогов команд видны в API панели."""
    srv = server_factory()
    base = f"http://127.0.0.1:{srv.args.web_port}"
    status = wait_for(lambda: _get_json(base + "/api/status"))
    assert status is not None
    settings = status["settings"]
    assert settings["greet_templates"]
    assert settings["command_stale_after"] >= settings["stale_after"]
    assert settings["announce_wait"] >= 0
    assert settings["stream_limit"] >= 1
    spec = _get_json(base + "/api/settings")
    assert spec["spec"]["greet_templates"]["type"] == "text"
    assert spec["spec"]["greet_template"]["hidden"] is True
