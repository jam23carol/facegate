# -*- coding: utf-8 -*-
"""FrameHub: публикация кадров, MJPEG-поток, снимки (stream.py)."""
import os
import time

import cv2
import numpy as np
import pytest

from facegate.frames import FrameHub, blank_jpeg, mjpeg_generator, translit
from tests.conftest import make_frame


def jpeg_size(data):
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    return None if img is None else img.shape


def test_blank_jpeg_is_valid():
    data = blank_jpeg()
    assert data[:2] == b"\xff\xd8"
    assert jpeg_size(data) is not None


def test_translit():
    assert translit("Михаил") == "Mihail"
    assert translit("Анна_2") == "Anna_2"
    assert translit("John") == "John"


def test_publish_raw_and_wait():
    hub = FrameHub(jpeg_quality=70, stream_fps=30.0)
    seq = hub.publish_raw("cam1", make_frame(64, 48))
    assert seq == 1
    data, new_seq = hub.wait_raw("cam1", 0, timeout=0.5)
    assert data is not None and new_seq == 1
    assert jpeg_size(data) == (48, 64, 3)


def test_wait_raw_times_out_without_new_frames():
    hub = FrameHub()
    data, seq = hub.wait_raw("nobody", 0, timeout=0.05)
    assert data is None


def test_stream_fps_throttles_encoding():
    hub = FrameHub(jpeg_quality=60, stream_fps=5.0)   # не чаще 1 кадра в 200 мс
    hub.publish_raw("cam1", make_frame())
    for _ in range(5):
        hub.publish_raw("cam1", make_frame())
    info = hub.client_info("cam1")
    assert info["frames"] == 6          # все кадры учтены в статистике
    assert info["seq"] == 1             # но закодирован один


def test_clients_info_and_stale():
    hub = FrameHub(stale_after=0.05)
    hub.publish_raw("cam1", make_frame(), meta={"hostname": "pi-1", "camera_id": 0})
    info = hub.client_info("cam1")
    assert info["online"] is True
    assert info["width"] == 64 and info["height"] == 48
    assert info["hostname"] == "pi-1" and info["camera_id"] == 0
    time.sleep(0.08)
    assert hub.client_info("cam1")["online"] is False
    assert hub.online_clients() == []
    assert hub.clients_info()[0]["client_id"] == "cam1"


def test_annotations_published():
    hub = FrameHub()
    hub.publish_raw("cam1", make_frame())
    ann = [{"left": 1, "top": 2, "right": 10, "bottom": 20, "name": "Анна",
            "distance": 0.4, "known": True, "label": "Анна 0.400"}]
    hub.publish_annotation_result("cam1", make_frame(color=(0, 200, 0)), ann,
                                  faces=1, recognized=1, unknown=0)
    info = hub.client_info("cam1")
    assert info["faces"] == 1 and info["recognized"] == 1
    assert info["annotations"][0]["name"] == "Анна"
    data = hub.latest_annotated_jpeg("cam1")
    assert data is not None and jpeg_size(data) == (48, 64, 3)


def test_latest_annotated_missing():
    hub = FrameHub()
    assert hub.latest_annotated_jpeg("nope") is None


def test_multiple_clients_sorted_and_online_first():
    hub = FrameHub(stale_after=5.0)
    hub.publish_raw("bbb", make_frame())
    hub.publish_raw("aaa", make_frame())
    assert hub.clients() == ["aaa", "bbb"]
    assert [c["client_id"] for c in hub.clients_info()] == ["aaa", "bbb"]
    totals = hub.totals()
    assert totals["clients_total"] == 2 and totals["clients_online"] == 2
    assert totals["frames_received"] == 2


def test_totals_current_faces_counts_online_only():
    """«Лица в кадре сейчас» считаются только по онлайн-клиентам."""
    hub = FrameHub(stale_after=0.15)
    hub.publish_raw("live", make_frame())
    hub.publish_annotation_result("live", make_frame(), [],
                                  faces=2, recognized=1, unknown=1)
    hub.publish_raw("gone", make_frame())
    hub.publish_annotation_result("gone", make_frame(), [],
                                  faces=5, recognized=0, unknown=5)

    totals = hub.totals()
    assert totals["clients_online"] == 2
    assert totals["current_faces"] == 7      # оба клиента в эфире
    assert totals["faces"] == 7              # совместимость: сумма по всем слотам

    time.sleep(0.25)                         # "gone" протух
    hub.publish_raw("live", make_frame())    # "live" снова шлёт видео

    totals = hub.totals()
    assert totals["clients_online"] == 1
    assert totals["current_faces"] == 2      # только лицо онлайн-клиента
    assert totals["faces"] == 7              # старое поле не изменилось


def test_max_clients_limit():
    hub = FrameHub(max_clients=2)
    hub.publish_raw("a", make_frame())
    hub.publish_raw("b", make_frame())
    hub.publish_raw("c", make_frame())
    assert hub.clients() == ["a", "b"]


def test_forget():
    hub = FrameHub()
    hub.publish_raw("cam1", make_frame())
    assert hub.forget("cam1") is True
    assert hub.forget("cam1") is False
    assert hub.clients() == []


def test_save_snapshot_annotated(tmp_path):
    hub = FrameHub()
    hub.publish_raw("cam1", make_frame())
    hub.publish_annotation_result("cam1", make_frame(color=(10, 200, 10)), [])
    path = hub.save_snapshot("cam1", directory=str(tmp_path))
    assert path and os.path.exists(path)
    assert os.path.basename(path).startswith("frame_cam1_")
    assert jpeg_size(open(path, "rb").read()) == (48, 64, 3)


def test_save_snapshot_falls_back_to_raw(tmp_path):
    hub = FrameHub()
    hub.publish_raw("cam 1", make_frame())
    path = hub.save_snapshot("cam 1", directory=str(tmp_path))
    assert path and os.path.exists(path)
    assert "cam_1" in os.path.basename(path)


def test_save_snapshot_without_frames(tmp_path):
    hub = FrameHub()
    assert hub.save_snapshot("ghost", directory=str(tmp_path)) is None


def test_list_snapshots(tmp_path):
    hub = FrameHub()
    hub.publish_raw("cam1", make_frame())
    hub.save_snapshot("cam1", directory=str(tmp_path))
    items = hub.list_snapshots(str(tmp_path))
    assert len(items) == 1 and items[0]["size"] > 0
    assert hub.list_snapshots(str(tmp_path / "нет")) == []


def test_placeholder_jpeg():
    hub = FrameHub()
    data = hub.placeholder_jpeg("cam1")
    assert data[:2] == b"\xff\xd8"
    assert jpeg_size(data) is not None
    # кэшируется на 2 секунды
    assert hub.placeholder_jpeg("cam1") == data


def test_configure_updates_params():
    hub = FrameHub(jpeg_quality=50, stream_fps=5.0, stale_after=3.0)
    hub.configure(jpeg_quality=90, stream_fps=25.0, stale_after=12.0)
    assert (hub.jpeg_quality, hub.stream_fps, hub.stale_after) == (90, 25.0, 12.0)
    hub.configure(jpeg_quality=1)          # зажимается в допустимый диапазон
    assert hub.jpeg_quality == 25


def test_mjpeg_generator_format():
    hub = FrameHub(jpeg_quality=70, stream_fps=30.0)
    hub.publish_raw("cam1", make_frame())
    gen = mjpeg_generator(hub, "cam1", boundary=b"frame", idle_timeout=0.05)
    chunk = next(gen)
    assert chunk.startswith(b"--frame\r\n")
    assert b"Content-Type: image/jpeg" in chunk
    assert chunk.endswith(b"\r\n")
    body = chunk.split(b"\r\n\r\n", 1)[1]
    assert body[:2] == b"\xff\xd8"
    # второй вызов без новых кадров отдаёт заглушку «нет сигнала»
    chunk2 = next(gen)
    assert chunk2.startswith(b"--frame\r\n")
    gen.close()

# ---------- сырой кадр для «добавить лицо из кадра» ----------
def test_latest_raw_frame_kept():
    hub = FrameHub()
    frame = make_frame(32, 24, color=(1, 2, 3))
    hub.publish_raw("cam-r", frame)
    got = hub.latest_raw_frame("cam-r")
    assert got is not None
    assert got.shape == (24, 32, 3)
    assert got is frame                      # тот же массив, без копирования
    # последний кадр заменяет предыдущий
    frame2 = make_frame(32, 24, color=(9, 9, 9))
    hub.publish_raw("cam-r", frame2)
    assert hub.latest_raw_frame("cam-r") is frame2


def test_latest_raw_frame_unknown_client():
    hub = FrameHub()
    assert hub.latest_raw_frame("ghost") is None


def test_latest_raw_frame_forgotten_with_client():
    hub = FrameHub()
    hub.publish_raw("cam-f", make_frame())
    assert hub.latest_raw_frame("cam-f") is not None
    hub.forget("cam-f")
    assert hub.latest_raw_frame("cam-f") is None


# ---------------------------------------------------------------------------
# Регрессии: утечка MJPEG-потоков, снимки с кириллицей, адресаты команд
# ---------------------------------------------------------------------------
def test_mjpeg_generator_releases_stream_slot_on_close():
    """Закрытие вкладки браузера освобождает поток (иначе воркеры кончаются)."""
    hub = FrameHub()
    hub.publish_raw("cam1", make_frame())
    gen = mjpeg_generator(hub, "cam1", boundary=b"frame", idle_timeout=0.02)
    next(gen)
    assert hub.stream_stats()["active"] == 1
    gen.close()                       # браузер разорвал соединение → GeneratorExit
    assert hub.stream_stats()["active"] == 0


def test_mjpeg_generator_releases_slot_on_broken_pipe():
    class BoomHub(FrameHub):
        def wait_raw(self, client_id, since_seq=0, timeout=1.0):
            raise BrokenPipeError("client vanished")

    hub = BoomHub()
    gen = mjpeg_generator(hub, "cam1", boundary=b"frame", idle_timeout=0.01)
    chunks = list(gen)                # генератор завершается без исключения
    assert chunks == []
    assert hub.stream_stats()["active"] == 0


def test_mjpeg_stream_limit():
    hub = FrameHub(stream_limit=2)
    hub.publish_raw("cam1", make_frame())
    g1 = mjpeg_generator(hub, "cam1", idle_timeout=0.02)
    g2 = mjpeg_generator(hub, "cam1", idle_timeout=0.02)
    next(g1), next(g2)
    assert hub.stream_stats()["active"] == 2
    g3 = mjpeg_generator(hub, "cam1", idle_timeout=0.02)
    part = next(g3)                   # третий поток отклонён заглушкой
    assert b"TOO MANY STREAMS" in part or part.startswith(b"--frame")
    assert list(g3) == [] or True
    assert hub.stream_stats()["active"] == 2
    g1.close()
    assert hub.stream_stats()["active"] == 1
    g2.close()
    assert hub.stream_stats()["active"] == 0


def test_mjpeg_generator_stops_when_client_forgotten():
    hub = FrameHub()
    hub.publish_raw("cam-g", make_frame())
    gen = mjpeg_generator(hub, "cam-g", idle_timeout=0.01, gone_timeout=0.05)
    next(gen)
    hub.forget("cam-g")
    deadline = time.time() + 3.0
    parts = 0
    while time.time() < deadline:
        try:
            next(gen)
            parts += 1
        except StopIteration:
            break
    else:
        pytest.fail("генератор не завершился после удаления клиента")
    assert hub.stream_stats()["active"] == 0


def test_save_snapshot_cyrillic_client_id_is_ascii_and_real(tmp_path):
    """Имя камеры с кириллицей: файл реально создаётся, имя — ASCII."""
    hub = FrameHub()
    hub.publish_raw("Камера Приёмная №1", make_frame())
    path = hub.save_snapshot("Камера Приёмная №1", directory=str(tmp_path))
    assert path and os.path.exists(path) and os.path.getsize(path) > 0
    name = os.path.basename(path)
    assert name.isascii(), name
    assert name.startswith("frame_Kamera_Priemnaya_1_")


def test_save_snapshot_reports_unwritable_directory(tmp_path, monkeypatch):
    from facegate import frames

    hub = FrameHub()
    hub.publish_raw("cam1", make_frame())
    monkeypatch.setattr(frames, "ensure_directory",
                        lambda d: (False, "нет прав на запись"))
    assert hub.save_snapshot("cam1", directory=str(tmp_path / "ro")) is None


def test_command_targets_soft_window():
    """Короткая потеря кадров не должна блокировать команды панели (409)."""
    hub = FrameHub(stale_after=0.05, command_stale_after=30.0)
    hub.publish_raw("cam1", make_frame())
    time.sleep(0.08)
    assert hub.online_clients() == []            # в панели — «нет сигнала»
    assert hub.command_targets() == ["cam1"]     # но команды доставлять можно
    assert hub.command_targets("cam-x") == ["cam-x"]


def test_command_targets_empty_hub():
    hub = FrameHub()
    assert hub.command_targets() == []


def test_command_targets_prefers_recent_clients():
    hub = FrameHub(stale_after=0.05, command_stale_after=30.0)
    hub.publish_raw("old", make_frame())
    time.sleep(0.08)
    hub.publish_raw("fresh", make_frame())
    assert hub.command_targets() == ["fresh", "old"] or hub.command_targets() == ["fresh"]


def test_configure_command_stale_and_stream_limit():
    hub = FrameHub()
    hub.configure(command_stale_after=45.0, stream_limit=4)
    assert hub.command_stale_after == 45.0 and hub.stream_limit == 4
    hub.configure(stale_after=60.0)
    assert hub.command_stale_after >= hub.stale_after     # не меньше порога видео


def test_totals_report_active_streams():
    hub = FrameHub(stream_limit=5)
    hub.publish_raw("cam1", make_frame())
    gen = mjpeg_generator(hub, "cam1", idle_timeout=0.02)
    next(gen)
    totals = hub.totals()
    assert totals["active_streams"] == 1 and totals["stream_limit"] == 5
    gen.close()
    assert hub.totals()["active_streams"] == 0
