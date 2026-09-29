# -*- coding: utf-8 -*-
"""Глобальная блокировка инференса dlib (защита от дедлока сервера).

Регрессия: «при попытке добавить неизвестное лицо прямо из интерфейса весь
сервер полностью зависает». Причина — параллельные вызовы непотокобезопасных
C++-моделей dlib из потока распознавания и из HTTP-воркера Flask (общий
OpenMP/BLAS-рантайм → взаимная блокировка на futex).

Проверяется, что:
  * все вызовы dlib сериализуются одной блокировкой (пересечений нет);
  * ожидание блокировки ограничено таймаутом → :class:`InferenceBusyError`,
    а не вечный «вис»;
  * блокировка реентерабельна (вложенные вызовы в одном потоке не клинят);
  * сценарий «распознавание + добавление лица из кадра» завершается, а не
    блокирует процесс.
"""
import threading
import time

import numpy as np
import pytest

from facegate.recognition import engine as recog
from facegate.recognition.engine import (InferenceBusyError, inference_lock,
                                         inference_locked)


def test_inference_lock_is_exclusive():
    """Два потока не могут находиться внутри dlib-вызова одновременно."""
    inside = []
    overlaps = []
    barrier = threading.Barrier(2, timeout=5.0)

    def worker(tag):
        barrier.wait()
        for _ in range(30):
            with inference_lock(timeout=5.0, who=tag):
                inside.append(tag)
                time.sleep(0.001)
                # если бы блокировка не работала, здесь был бы чужой тег
                if inside[-1] != tag:
                    overlaps.append(list(inside[-2:]))
                inside.pop()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15.0)
        assert not t.is_alive(), "поток не завершился — блокировка заклинила"
    assert overlaps == []


def test_inference_lock_timeout_raises_busy():
    """Ожидание ограничено: вместо зависания — понятная ошибка."""
    holder_ready = threading.Event()
    release = threading.Event()

    def holder():
        with inference_lock(timeout=0, who="holder"):
            holder_ready.set()
            release.wait(timeout=10.0)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert holder_ready.wait(timeout=5.0)
    started = time.time()
    with pytest.raises(InferenceBusyError) as ei:
        with inference_lock(timeout=0.2, who="flask-worker"):
            pass  # pragma: no cover - сюда попасть не должны
    elapsed = time.time() - started
    release.set()
    t.join(timeout=5.0)
    assert elapsed < 3.0, "ожидание блокировки не ограничено таймаутом"
    assert "занят" in str(ei.value)


def _probe_from_other_thread(timeout=0.0):
    """Занята ли блокировка с точки зрения ДРУГОГО потока (RLock реентерабелен)."""
    result = {}

    def probe():
        result["locked"] = inference_locked(timeout=timeout)

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(timeout=5.0)
    return result.get("locked")


def test_inference_lock_is_reentrant():
    """Вложенные вызовы в одном потоке не приводят к самоблокировке."""
    with inference_lock(timeout=1.0, who="outer"):
        with inference_lock(timeout=1.0, who="inner"):
            assert _probe_from_other_thread() is True


def test_inference_locked_reports_state():
    assert inference_locked(timeout=0) is False
    with inference_lock(timeout=1.0):
        assert _probe_from_other_thread() is True
    assert _probe_from_other_thread() is False


def test_detect_helpers_go_through_lock(monkeypatch, fake_fr):
    """Обёртки face_locations/face_encodings действительно берут блокировку."""
    seen = []
    original = fake_fr.face_locations

    def spy_locations(img, model="hog"):
        seen.append(("locations", _probe_from_other_thread()))
        return original(img, model)

    monkeypatch.setattr(fake_fr, "face_locations", spy_locations)
    rgb = np.zeros((32, 32, 3), dtype=np.uint8)
    recog.face_locations(rgb)
    assert seen and seen[0][1] is True


def test_capture_and_recognition_do_not_deadlock(fake_fr, monkeypatch):
    """Главная регрессия: детекция для панели параллельно с потоком видео.

    Имитируем ситуацию «пользователь нажал +лицо из кадра, пока работает
    распознавание»: оба потока должны завершиться, а не заблокировать процесс.
    """
    calls = {"n": 0}
    original = fake_fr.face_locations     # ВАЖНО: до подмены, иначе рекурсия

    def slow_locations(img, model="hog"):
        calls["n"] += 1
        time.sleep(0.01)          # «инференс» dlib
        return original(img, model)

    monkeypatch.setattr(fake_fr, "face_locations", slow_locations)
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    errors = []
    stop = threading.Event()

    def recognition_loop():
        # polite=True — как в настоящем потоке распознавания: уступаем движок
        # запросам панели, которые уже ждут блокировку
        try:
            while not stop.is_set():
                rgb = np.zeros_like(frame)
                recog.detect_with_embeddings(rgb, timeout=2.0, polite=True)
        except Exception as e:  # noqa: BLE001
            errors.append(f"recognition: {e}")

    def panel_worker():
        try:
            for _ in range(10):
                recog.detect_faces_for_capture(frame, timeout=5.0)
        except Exception as e:  # noqa: BLE001
            errors.append(f"panel: {e}")

    rec = threading.Thread(target=recognition_loop, daemon=True, name="recognition")
    rec.start()
    panel = threading.Thread(target=panel_worker, daemon=True, name="flask-worker")
    panel.start()

    panel.join(timeout=20.0)
    stop.set()
    rec.join(timeout=10.0)

    assert not panel.is_alive(), "HTTP-воркер завис — сервер перестал бы отвечать"
    assert not rec.is_alive(), "поток распознавания завис"
    assert errors == [], f"потоки получили ошибки: {errors}"
    assert calls["n"] > 10
