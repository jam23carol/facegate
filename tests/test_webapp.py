# -*- coding: utf-8 -*-
import io
import os

import cv2
import numpy as np


def png_bytes_raw():
    ok, buf = cv2.imencode(".png", np.zeros((40, 40, 3), dtype=np.uint8))
    assert ok
    return buf.tobytes()


def png_upload(name, filename="photo.png"):
    data = png_bytes_raw()
    return {"name": name, "photos": [(io.BytesIO(data), filename)]}


def test_index_page(client_pair):
    app, _ = client_pair
    res = app.get("/")
    assert res.status_code == 200
    assert "Распознавание лиц".encode("utf-8") in res.data


def test_persons_empty(client_pair):
    app, _ = client_pair
    res = app.get("/api/persons")
    assert res.status_code == 200
    assert res.json["persons"] == []


def test_add_and_list_person(client_pair):
    app, store = client_pair
    res = app.post("/api/persons", data=png_upload("Тест Тест"),
                   content_type="multipart/form-data")
    assert res.status_code == 200
    body = res.json
    assert body["ok"] is True
    assert body["results"][0]["ok"] is True
    assert body["voice"] == "ready"

    persons = app.get("/api/persons").json["persons"]
    assert len(persons) == 1
    assert persons[0]["name"] == "Тест Тест"
    assert persons[0]["voice_status"] == "ready"
    assert persons[0]["photos"] == ["Тест Тест.jpg"]
    assert os.path.exists(os.path.join(store.faces_dir, "Тест Тест.jpg"))


def test_add_multiple_photos_one_person(client_pair):
    app, store = client_pair
    form = png_upload("Анна")
    form["photos"].append((io.BytesIO(png_bytes_raw()), "b.png"))
    res = app.post("/api/persons", data=form, content_type="multipart/form-data")
    assert res.status_code == 200
    assert len(res.json["results"]) == 2
    assert sorted(store.photos("Анна")) == ["Анна.jpg", "Анна_2.jpg"]


def test_add_garbage_bytes_rejected(client_pair):
    app, _ = client_pair
    res = app.post("/api/persons",
                   data={"name": "Боб", "photos": [(io.BytesIO(b"junk"), "j.png")]},
                   content_type="multipart/form-data")
    assert res.status_code == 422
    assert res.json["ok"] is False
    assert "декодировать" in res.json["results"][0]["message"]


def test_add_empty_name_400(client_pair):
    app, _ = client_pair
    res = app.post("/api/persons", data=png_upload("   "),
                   content_type="multipart/form-data")
    assert res.status_code == 400


def test_delete_person(client_pair):
    app, store = client_pair
    app.post("/api/persons", data=png_upload("Михаил"), content_type="multipart/form-data")
    fname = store.photos("Михаил")[0]
    res = app.delete("/api/persons/Михаил")
    assert res.status_code == 200
    assert res.json["ok"] is True
    assert app.get("/api/persons").json["persons"] == []
    assert not os.path.exists(os.path.join(store.faces_dir, fname))


def test_delete_unknown_404(client_pair):
    app, _ = client_pair
    assert app.delete("/api/persons/Никто").status_code == 404


def test_resynthesize(client_pair):
    app, _ = client_pair
    app.post("/api/persons", data=png_upload("Анна"), content_type="multipart/form-data")
    res = app.post("/api/persons/Анна/resynthesize")
    assert res.status_code == 200
    assert res.json["voice"] == "ready"
    assert res.json["phrase"] == "Здравствуйте, Анна!"
    assert app.post("/api/persons/Нет/resynthesize").status_code == 404


def test_photo_serving_and_traversal_guard(client_pair):
    app, store = client_pair
    app.post("/api/persons", data=png_upload("Анна"), content_type="multipart/form-data")
    fname = store.photos("Анна")[0]
    res = app.get(f"/photo/{fname}")
    assert res.status_code == 200
    assert res.content_type.startswith("image/")
    assert app.get("/photo/..%2fserver.py").status_code == 404
    assert app.get("/photo/readme.txt").status_code == 404


def test_voice_failure_reported(client_pair_factory, failing_dummy_voice):
    app, _ = client_pair_factory(voice=failing_dummy_voice)
    res = app.post("/api/persons", data=png_upload("Кэрол"),
                   content_type="multipart/form-data")
    assert res.status_code == 200
    assert res.json["voice"] == "error"
