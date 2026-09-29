# -*- coding: utf-8 -*-
"""Добавление нового лица прямо из текущего кадра камеры.

API:
  GET  /api/cameras/<id>/faces      — какие лица сейчас в кадре;
  GET  /api/cameras/<id>/face.jpg   — превью-кроп лица;
  POST /api/persons/from-frame      — добавить человека из кадра.
"""
import io
import os

import cv2
import numpy as np
import pytest

from tests.conftest import make_frame


def publish(client_deps, client_id="cam-a", frame=None):
    client_deps["hub"].publish_raw(client_id, frame if frame is not None
                                   else make_frame(160, 120))


def png_bytes():
    ok, buf = cv2.imencode(".png", np.zeros((40, 40, 3), dtype=np.uint8))
    assert ok
    return buf.tobytes()


def set_multi_faces(fake_fr, boxes):
    """Учит заглушку face_recognition возвращать несколько лиц."""
    fake_fr.face_locations = lambda img, model="hog": list(boxes)

    def encodings(img, locs=None):
        out = []
        for i in range(len(locs or boxes)):
            vec = np.zeros(128, dtype=np.float32)
            vec[0] = 0.001 * (i + 1)
            out.append(vec)
        return out

    fake_fr.face_encodings = encodings


# ---------------------------------------------------------------------------
# GET /api/cameras/<id>/faces
# ---------------------------------------------------------------------------
def test_faces_in_current_frame(full_app):
    client, deps = full_app
    publish(deps)
    res = client.get("/api/cameras/cam-a/faces")
    assert res.status_code == 200
    body = res.json
    assert body["width"] == 160 and body["height"] == 120
    assert len(body["faces"]) == 1
    face = body["faces"][0]
    assert face["known"] is False           # база эталонов пуста
    assert face["left"] == 0 and face["top"] == 0
    assert face["right"] == 4 and face["bottom"] == 4
    assert "preview_url" in face


def test_faces_marks_known_person(full_app):
    client, deps = full_app
    deps["store"].add_photo("Анна", png_bytes())
    publish(deps)
    res = client.get("/api/cameras/cam-a/faces")
    face = res.json["faces"][0]
    assert face["known"] is True
    assert face["name"] == "Анна"


def test_faces_without_frames_404(full_app):
    client, _deps = full_app
    assert client.get("/api/cameras/ghost/faces").status_code == 404


def test_faces_no_faces_detected(full_app, fake_fr):
    client, deps = full_app
    fake_fr.faces_present = False
    publish(deps)
    res = client.get("/api/cameras/cam-a/faces")
    assert res.status_code == 200
    assert res.json["faces"] == []


# ---------------------------------------------------------------------------
# GET /api/cameras/<id>/face.jpg
# ---------------------------------------------------------------------------
def test_face_crop_jpeg(full_app):
    client, deps = full_app
    publish(deps, frame=make_frame(160, 120, color=(10, 200, 30)))
    res = client.get("/api/cameras/cam-a/face.jpg?left=20&top=10&right=60&bottom=50")
    assert res.status_code == 200
    assert res.mimetype == "image/jpeg"
    img = cv2.imdecode(np.frombuffer(res.data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    # кроп с отступом margin=0.35: 40px по ширине → +14px с каждой стороны
    assert img.shape[1] > 40 and img.shape[0] > 40


def test_face_crop_bad_params(full_app):
    client, deps = full_app
    publish(deps)
    assert client.get("/api/cameras/cam-a/face.jpg").status_code == 400
    assert client.get("/api/cameras/cam-a/face.jpg?left=50&top=0&right=10&bottom=5").status_code == 400
    assert client.get("/api/cameras/ghost/face.jpg?left=0&top=0&right=5&bottom=5").status_code == 404


# ---------------------------------------------------------------------------
# POST /api/persons/from-frame
# ---------------------------------------------------------------------------
def test_add_person_from_frame_explicit_box(full_app):
    client, deps = full_app
    publish(deps)
    res = client.post("/api/persons/from-frame", json={
        "client_id": "cam-a", "name": "Иван Петров",
        "left": 20, "top": 10, "right": 60, "bottom": 50})
    assert res.status_code == 200, res.json
    body = res.json
    assert body["ok"] is True
    assert body["name"] == "Иван Петров"
    assert body["voice"] == "ready"                    # озвучка встала в очередь/готова
    # человек реально добавлен в базу, файл создан
    assert deps["store"].has("Иван Петров")
    photos = deps["store"].photos("Иван Петров")
    assert len(photos) == 1
    assert os.path.exists(os.path.join(deps["store"].faces_dir, photos[0]))
    # появился в списке людей и в статистике
    persons = client.get("/api/persons").json["persons"]
    assert [p["name"] for p in persons] == ["Иван Петров"]
    assert deps["stats"].counters()["faces_captured"] == 1


def test_add_person_from_frame_autodetect_single_face(full_app):
    client, deps = full_app
    publish(deps)
    res = client.post("/api/persons/from-frame",
                      json={"client_id": "cam-a", "name": "Авто Detect"})
    assert res.status_code == 200, res.json
    assert deps["store"].has("Авто Detect")


def test_add_person_from_frame_multiple_faces_409(full_app, fake_fr):
    client, deps = full_app
    set_multi_faces(fake_fr, [(0, 4, 4, 0), (10, 60, 50, 20)])
    publish(deps)
    res = client.post("/api/persons/from-frame",
                      json={"client_id": "cam-a", "name": "Кто-то"})
    assert res.status_code == 409
    assert len(res.json["faces"]) == 2
    assert not deps["store"].has("Кто-то")


def test_add_person_from_frame_no_faces_422(full_app, fake_fr):
    client, deps = full_app
    fake_fr.faces_present = False
    publish(deps)
    res = client.post("/api/persons/from-frame",
                      json={"client_id": "cam-a", "name": "Призрак"})
    assert res.status_code == 422


def test_add_person_from_frame_validation(full_app):
    client, deps = full_app
    publish(deps)
    # пустое имя
    res = client.post("/api/persons/from-frame",
                      json={"client_id": "cam-a", "name": "  "})
    assert res.status_code == 400
    # неизвестная камера
    res = client.post("/api/persons/from-frame",
                      json={"client_id": "ghost", "name": "Вася",
                            "left": 0, "top": 0, "right": 4, "bottom": 4})
    assert res.status_code == 404
    # нет client_id
    res = client.post("/api/persons/from-frame", json={"name": "Вася"})
    assert res.status_code == 400


def test_add_from_frame_second_photo_same_name(full_app):
    client, deps = full_app
    publish(deps)
    box = {"left": 20, "top": 10, "right": 60, "bottom": 50}
    client.post("/api/persons/from-frame", json={"client_id": "cam-a",
                                                 "name": "Анна", **box})
    res = client.post("/api/persons/from-frame", json={"client_id": "cam-a",
                                                       "name": "Анна", **box})
    assert res.status_code == 200
    assert sorted(deps["store"].photos("Анна")) == ["Анна.jpg", "Анна_2.jpg"]


def test_add_from_frame_pushes_event(full_app):
    client, deps = full_app
    publish(deps)
    client.post("/api/persons/from-frame", json={
        "client_id": "cam-a", "name": "Событийный",
        "left": 0, "top": 0, "right": 4, "bottom": 4})
    events, _ = deps["events"].since(0)
    added = [e for e in events if e["type"] == "person_added"]
    assert added and added[-1]["source"] == "frame"
    assert "Событийный" in added[-1]["text"]


# ---------------------------------------------------------------------------
# Регрессии: «добавить неизвестное лицо из интерфейса» вешало весь сервер
# ---------------------------------------------------------------------------
from facegate.recognition import engine as recog


class FakeDlib:
    """Мини-dlib: нужен, чтобы эмбеддинг считался по рамке исходного кадра."""

    class rectangle:
        def __init__(self, left, top, right, bottom):
            self.left, self.top = left, top
            self.right, self.bottom = right, bottom


@pytest.fixture
def fake_dlib(monkeypatch):
    monkeypatch.setattr(recog, "dlib", FakeDlib)
    return FakeDlib


def test_add_from_frame_does_not_redetect_tight_crop(full_app, fake_fr, fake_dlib):
    """Ключевая регрессия: лицо из кадра добавляется без повторной детекции.

    HOG-детектор на плотно обрезанном кропе почти всегда возвращает 0 лиц —
    раньше из-за этого панель отвечала 422, а файл в known_faces/ не создавался.
    """
    client, deps = full_app
    publish(deps)
    face = client.get("/api/cameras/cam-a/faces").json["faces"][0]
    box = {k: face[k] for k in ("left", "top", "right", "bottom")}

    detections = {"n": 0}
    original = fake_fr.face_locations

    def counting_locations(img, model="hog"):
        detections["n"] += 1
        return original(img, model)

    fake_fr.face_locations = counting_locations
    # детектор «сломался» на кропах — как в реальности для обрезанного лица
    before = detections["n"]
    fake_fr.faces_present = False

    res = client.post("/api/persons/from-frame",
                      json={"client_id": "cam-a", "name": "Неизвестный Гость", **box})
    assert res.status_code == 200, res.json
    assert deps["store"].has("Неизвестный Гость")
    photos = deps["store"].photos("Неизвестный Гость")
    assert len(photos) == 1
    assert os.path.exists(os.path.join(deps["store"].faces_dir, photos[0]))
    # повторная детекция на кропе НЕ выполнялась (эмбеддинг взят по рамке)
    assert detections["n"] == before


def test_add_from_frame_stores_embedding_from_box(full_app, fake_fr, fake_dlib):
    client, deps = full_app
    publish(deps)
    res = client.post("/api/persons/from-frame", json={
        "client_id": "cam-a", "name": "С Эмбеддингом",
        "left": 20, "top": 10, "right": 60, "bottom": 50})
    assert res.status_code == 200, res.json
    entries = deps["store"].entries()
    assert len(entries) == 1
    name, enc = entries[0]
    assert name == "С Эмбеддингом"
    assert isinstance(enc, np.ndarray) and enc.shape == (128,)


def test_add_from_frame_busy_engine_returns_503(full_app, monkeypatch):
    """Движок занят потоком видео → 503 с понятным текстом, сервер жив."""
    client, deps = full_app
    publish(deps)

    def busy(*args, **kwargs):
        raise recog.InferenceBusyError(
            "Движок распознавания занят (faces): не удалось получить доступ за 30 с.")

    monkeypatch.setattr(recog, "detect_faces_for_capture", busy)
    res = client.get("/api/cameras/cam-a/faces")
    assert res.status_code == 503
    assert res.json["busy"] is True and "занят" in res.json["error"]

    monkeypatch.setattr(recog, "embedding_from_box", busy)
    res = client.post("/api/persons/from-frame", json={
        "client_id": "cam-a", "name": "Занято",
        "left": 1, "top": 1, "right": 20, "bottom": 20})
    assert res.status_code == 503
    assert not deps["store"].has("Занято")
    # панель продолжает отвечать на остальные запросы
    assert client.get("/api/health").status_code == 200


def test_add_from_frame_saves_photo_with_context(full_app, fake_fr, fake_dlib):
    """Сохранённый кроп имеет поля: эталон находится детектором и после рестарта."""
    client, deps = full_app
    publish(deps, frame=make_frame(320, 240))
    face = client.get("/api/cameras/cam-a/faces").json["faces"][0]
    box_w = face["right"] - face["left"]
    res = client.post("/api/persons/from-frame", json={
        "client_id": "cam-a", "name": "С Полями",
        **{k: face[k] for k in ("left", "top", "right", "bottom")}})
    assert res.status_code == 200, res.json
    photo = deps["store"].photos("С Полями")[0]
    img = cv2.imread(os.path.join(deps["store"].faces_dir, photo))
    assert img is not None
    assert img.shape[1] >= box_w          # кроп сохранён с отступами, не «в обрез»


def test_face_crop_download_disposition(full_app):
    """«Скачать кадр» сохраняет файл, а не открывает JPEG в текущей вкладке."""
    client, deps = full_app
    publish(deps)
    inline = client.get("/api/cameras/cam-a/frame.jpg")
    assert "inline" in inline.headers.get("Content-Disposition", "")
    attachment = client.get("/api/cameras/cam-a/frame.jpg?download=1")
    disposition = attachment.headers.get("Content-Disposition", "")
    assert disposition.startswith("attachment"), disposition
    assert 'filename="' in disposition
