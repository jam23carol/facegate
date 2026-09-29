# -*- coding: utf-8 -*-
"""Саундборд: библиотека звуков (загрузки + синтезированная озвучка), скрытие, API."""
import io
import json
import os
import time
import wave
import zlib

import numpy as np
import pytest
import zmq

from facegate.soundboard import Soundboard, sanitize_filename
from tests.conftest import make_wav_bytes


@pytest.fixture
def sb(tmp_path, dummy_voice):
    board = Soundboard(sounds_dir=str(tmp_path / "soundboard"),
                       state_path=str(tmp_path / "sb_state.json"),
                       voice=dummy_voice)
    return board


# ---------------------------------------------------------------------------
# модуль Soundboard
# ---------------------------------------------------------------------------
def test_sanitize_filename():
    assert sanitize_filename("звук.wav") == "звук.wav"
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("a/b\\c.mp3") == "c.mp3"
    assert sanitize_filename("  ") == ""
    assert sanitize_filename("x" * 200 + ".wav").endswith(".wav")


def test_add_upload_and_list(sb):
    ok, item = sb.add_upload("звонок.wav", make_wav_bytes())
    assert ok is True
    assert item["id"] == "upload:звонок.wav"
    assert item["kind"] == "upload"
    assert item["mimetype"] == "audio/wav"
    items = sb.list_items()
    assert [i["id"] for i in items] == ["upload:звонок.wav"]
    assert os.path.isfile(item["path"])


def test_add_upload_duplicate_gets_suffix(sb):
    sb.add_upload("ding.wav", make_wav_bytes())
    ok, item = sb.add_upload("ding.wav", make_wav_bytes())
    assert ok and item["filename"] == "ding_2.wav"


def test_add_upload_rejects_bad_input(sb):
    assert sb.add_upload("вирус.exe", b"data")[0] is False
    assert sb.add_upload("пусто.wav", b"")[0] is False
    assert sb.add_upload("", make_wav_bytes())[0] is False
    big = b"0" * (26 * 1024 * 1024)
    assert sb.add_upload("big.wav", big)[0] is False


def test_hide_show_persisted(sb, tmp_path, dummy_voice):
    sb.add_upload("a.wav", make_wav_bytes())
    assert sb.list_items() != []
    sb.set_hidden("upload:a.wav", True)
    assert sb.list_items() == []                      # скрыт из обычного списка
    assert len(sb.list_items(include_hidden=True)) == 1
    # состояние переживает пересоздание объекта
    sb2 = Soundboard(sounds_dir=str(tmp_path / "soundboard"),
                     state_path=str(tmp_path / "sb_state.json"), voice=dummy_voice)
    assert sb2.is_hidden("upload:a.wav") is True
    sb2.set_hidden("upload:a.wav", False)
    assert len(sb2.list_items()) == 1


def test_delete_upload(sb):
    sb.add_upload("del.wav", make_wav_bytes())
    ok, _msg = sb.delete("upload:del.wav")
    assert ok is True
    assert sb.list_items() == []
    assert sb.delete("upload:del.wav")[0] is False     # повторное удаление — ошибка
    assert sb.delete("upload:../../etc/passwd")[0] is False


def test_synth_items_from_voice_cache(sb, dummy_voice):
    dummy_voice.ensure("Анна")
    items = sb.list_items()
    assert len(items) == 1
    item = items[0]
    assert item["id"].startswith("synth:")
    assert item["kind"] == "synth"
    assert item["name"] == "Анна"
    assert "Анна" in item["title"]
    # скрытие работает и для синтезированных
    sb.set_hidden(item["id"], True)
    assert sb.list_items() == []
    # удаление убирает файл озвучки
    sb.set_hidden(item["id"], False)
    ok, _msg = sb.delete(item["id"])
    assert ok is True
    assert dummy_voice.get("Анна") is None


def test_delete_synth_bad_digest(sb, dummy_voice):
    assert sb.delete("synth:zzzz")[0] is False
    assert sb.delete("synth:../x")[0] is False


def test_resolve_and_traversal_guard(sb, tmp_path):
    sb.add_upload("ok.wav", make_wav_bytes())
    item = sb.resolve("upload:ok.wav")
    assert item is not None and item["path"].endswith("ok.wav")
    assert sb.resolve("upload:../secret.wav") is None
    assert sb.resolve("unknown:x") is None
    assert sb.resolve("") is None


def test_wav_bytes_for_broadcast(sb):
    data = make_wav_bytes()
    sb.add_upload("play.wav", data)
    wav, err = sb.wav_bytes_for_broadcast("upload:play.wav")
    assert err is None and wav == data


def test_wav_bytes_for_broadcast_unknown(sb):
    wav, err = sb.wav_bytes_for_broadcast("upload:нет.wav")
    assert wav is None and err


def test_wav_bytes_non_wav_without_ffmpeg(sb, monkeypatch):
    sb.add_upload("song.mp3", b"ID3fake-mp3-bytes")
    monkeypatch.setattr(Soundboard, "ffmpeg_available", staticmethod(lambda: False))
    wav, err = sb.wav_bytes_for_broadcast("upload:song.mp3")
    assert wav is None
    assert "ffmpeg" in err


# ---------------------------------------------------------------------------
# HTTP API панели
# ---------------------------------------------------------------------------
def test_api_soundboard_list_empty(full_app_factory):
    app, deps = full_app_factory(with_soundboard=True)
    client = app.test_client()
    res = client.get("/api/soundboard")
    assert res.status_code == 200
    assert res.json["items"] == []
    assert res.json["dir"] == deps["soundboard"].sounds_dir


def test_api_soundboard_upload_hide_delete(full_app_factory):
    app, deps = full_app_factory(with_soundboard=True)
    client = app.test_client()
    wav = make_wav_bytes()
    res = client.post("/api/soundboard/upload",
                      data={"file": (io.BytesIO(wav), "гонг.wav")},
                      content_type="multipart/form-data")
    assert res.status_code == 200, res.json
    item_id = res.json["item"]["id"]
    assert item_id == "upload:гонг.wav"

    # виден в списке
    items = client.get("/api/soundboard").json["items"]
    assert [i["id"] for i in items] == [item_id]

    # файл отдаётся для проигрывания в браузере
    got = client.get(f"/api/soundboard/{item_id}/file")
    assert got.status_code == 200
    assert got.mimetype == "audio/wav"
    assert got.data == wav

    # скрытие
    res = client.post(f"/api/soundboard/{item_id}/hide", json={"hidden": True})
    assert res.status_code == 200 and res.json["hidden"] is True
    assert client.get("/api/soundboard").json["items"] == []
    assert len(client.get("/api/soundboard?include_hidden=1").json["items"]) == 1

    # удаление
    res = client.delete(f"/api/soundboard/{item_id}")
    assert res.status_code == 200 and res.json["ok"] is True
    assert client.get(f"/api/soundboard/{item_id}/file").status_code == 404


def test_api_soundboard_upload_bad_file(full_app_factory):
    app, _deps = full_app_factory(with_soundboard=True)
    client = app.test_client()
    res = client.post("/api/soundboard/upload",
                      data={"file": (io.BytesIO(b"MZ"), "malware.exe")},
                      content_type="multipart/form-data")
    assert res.status_code == 422
    assert client.post("/api/soundboard/upload", data={},
                       content_type="multipart/form-data").status_code == 400


def test_api_soundboard_includes_greetings(full_app_factory):
    app, deps = full_app_factory(with_soundboard=True)
    client = app.test_client()
    deps["voice"].ensure("Анна")
    items = client.get("/api/soundboard").json["items"]
    # по одному элементу на каждый вариант фразы из пула приветствий
    variants = len(deps["voice"].templates)
    assert len(items) == variants
    assert all(i["kind"] == "synth" and i["name"] == "Анна" for i in items)
    assert {i["title"] for i in items} == set(deps["voice"].texts_for("Анна"))


def test_api_soundboard_broadcast_sends_wav(full_app_factory):
    from tests.conftest import make_frame

    app, deps = full_app_factory(with_soundboard=True, with_sender=True)
    client = app.test_client()
    sender = deps["sender"]
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://127.0.0.1:{sender.bound_port}")
    sub.setsockopt_string(zmq.SUBSCRIBE, "cam-a")
    try:
        time.sleep(0.35)
        deps["hub"].publish_raw("cam-a", make_frame())     # клиент «в эфире»
        wav = make_wav_bytes()
        client.post("/api/soundboard/upload",
                    data={"file": (io.BytesIO(wav), "ding.wav")},
                    content_type="multipart/form-data")
        res = client.post("/api/soundboard/upload:ding.wav/broadcast", json={})
        assert res.status_code == 200, res.json
        assert res.json["sent"] == 1
        assert sub.poll(3000)
        topic = sub.recv_string()
        msg = json.loads(sub.recv_string())
        assert topic == "cam-a"
        assert msg["action"] == "speak"
        assert msg["text"] == "ding"
        import base64
        assert base64.b64decode(msg["audio_b64"]) == wav
        assert deps["stats"].counters()["soundboard_plays"] == 1
    finally:
        sub.close(0)
        ctx.term()


def test_api_soundboard_broadcast_no_clients(full_app_factory):
    app, _deps = full_app_factory(with_soundboard=True, with_sender=True)
    client = app.test_client()
    client.post("/api/soundboard/upload",
                data={"file": (io.BytesIO(make_wav_bytes()), "x.wav")},
                content_type="multipart/form-data")
    res = client.post("/api/soundboard/upload:x.wav/broadcast", json={})
    assert res.status_code == 409


def test_api_soundboard_broadcast_without_sender(full_app_factory):
    app, _deps = full_app_factory(with_soundboard=True)
    client = app.test_client()
    res = client.post("/api/soundboard/upload:x.wav/broadcast", json={})
    assert res.status_code == 503
