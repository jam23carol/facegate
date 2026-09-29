# -*- coding: utf-8 -*-
"""Приём кадров от клиентов и трансляция видео в админ-панель.

Сервер не открывает GUI-окон: кадры, приходящие от клиентов, попадают в
:class:`FrameHub`, а браузер получает их как MJPEG-поток (``/stream/<client_id>``)
плюс JSON-аннотации распознанных лиц, которые рисуются поверх потока.

Поток публикуется **сразу** в потоке приёма видео (поэтому картинка плавная,
с частотой клиента), а распознавание живёт в своём потоке и только обновляет
аннотации и «аннотированный» кадр для снимков.

Помимо JPEG-кадра хаб хранит последний **сырой** кадр (numpy) каждого клиента —
он используется для добавления нового лица прямо из текущего изображения
(``/api/persons/from-frame``) и для кропа превью (``/api/cameras/<id>/face.jpg``).

Что исправлено в этой версии
----------------------------
* **Утечка MJPEG-потоков**: генератор корректно обрабатывает ``GeneratorExit``
  (закрытие вкладки/перезагрузка страницы) и в ``finally`` освобождает слот
  потока. Каждый Werkzeug-поток, обслуживающий ``/stream/``, удерживается
  навсегда, поэтому утечка приводила к исчерпанию воркеров и «залипанию»
  всей панели. Добавлен учёт и лимит одновременных потоков
  (:attr:`FrameHub.stream_limit`).
* **Снимки не сохранялись**: ``cv2.imwrite`` молча возвращает ``False`` на
  путях с кириллицей (Windows). Запись идёт через :mod:`facegate.imaging`
  (``imencode`` + байты), имя файла приводится к ASCII, результат проверяется,
  а ошибки прав доступа возвращаются как понятный текст.
* **«Нет клиентов в эфире» (409)**: адресаты команд считаются по более мягкому
  таймауту :attr:`FrameHub.command_stale_after` (:meth:`FrameHub.command_targets`),
  поэтому короткая пропажа видеокадров не блокирует кнопки панели.
"""
import logging
import os
import threading
import time

import cv2
import numpy as np

from facegate.imaging import (ascii_safe_name, ensure_directory, encode_image,
                              translit, write_bytes, write_image)

log = logging.getLogger(__name__)

__all__ = ["FrameHub", "mjpeg_generator", "blank_jpeg", "translit",
           "ascii_safe_name"]

# Небольшой чёрный JPEG — заглушка, если кодирование кадра не удалось.
_BLANK_CACHE = {}


def blank_jpeg(width=32, height=32):
    cached = _BLANK_CACHE.get((width, height))
    if cached is not None:
        return cached
    img = np.zeros((max(1, height), max(1, width), 3), dtype=np.uint8)
    data = encode_image(img, ".jpg", 60) or b""
    _BLANK_CACHE[(width, height)] = data
    return data


_PLACEHOLDER_SIZE = (640, 360)

# Сколько секунд поток живёт без клиента в хабе, прежде чем завершиться
# (браузер переподключится сам — см. img.onerror в админ-панели).
STREAM_GONE_TIMEOUT = 120.0


class ClientSlot:
    """Состояние одного клиента (камеры)."""

    __slots__ = ("client_id", "raw_jpeg", "raw_frame", "annotated_frame", "annotations",
                 "width", "height", "seq_raw", "seq_ann", "last_raw", "last_ann",
                 "frames", "fps", "faces", "recognized", "unknown", "meta",
                 "first_seen", "last_encode", "cond")

    def __init__(self, client_id, cond):
        self.client_id = client_id
        self.cond = cond
        self.raw_jpeg = None
        self.raw_frame = None          # последний сырой кадр (numpy, BGR)
        self.annotated_frame = None    # последний кадр с рамками (numpy, BGR)
        self.annotations = []
        self.width = 0
        self.height = 0
        self.seq_raw = 0
        self.seq_ann = 0
        self.last_raw = 0.0
        self.last_ann = 0.0
        self.frames = 0
        self.fps = 0.0
        self.faces = 0
        self.recognized = 0
        self.unknown = 0
        self.meta = {}
        self.first_seen = time.time()
        self.last_encode = 0.0

    def public_info(self, stale_after=8.0, now=None):
        now = time.time() if now is None else now
        age = now - self.last_raw if self.last_raw else None
        meta = dict(self.meta or {})
        return {
            "client_id": self.client_id,
            "online": bool(self.last_raw and age <= stale_after),
            "has_video": self.raw_jpeg is not None,
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 1),
            "frames": self.frames,
            "faces": self.faces,
            "recognized": self.recognized,
            "unknown": self.unknown,
            "last_frame_age": round(age, 2) if age is not None else None,
            "last_annotation_age": round(now - self.last_ann, 2) if self.last_ann else None,
            "annotations": [dict(a) for a in self.annotations],
            "seq": self.seq_raw,
            "connected_at": self.first_seen,
            "hostname": meta.get("hostname"),
            "camera_id": meta.get("camera_id"),
            "client_fps": meta.get("fps"),
            "client_version": meta.get("version"),
            "meta": meta,
        }


class FrameHub:
    """Последние кадры и аннотации по каждому клиенту + генератор MJPEG."""

    def __init__(self, jpeg_quality=80, stream_fps=15.0, stale_after=8.0,
                 max_clients=32, command_stale_after=None, stream_limit=24):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._slots = {}
        self.jpeg_quality = int(jpeg_quality)
        self.stream_fps = float(stream_fps)
        self.stale_after = float(stale_after)
        # Для команд «в эфире» считается мягче, чем для индикации видео:
        # короткая потеря кадров не должна блокировать объявления и саундборд.
        self.command_stale_after = (float(command_stale_after) if command_stale_after
                                    else max(self.stale_after * 4.0, 30.0))
        self.max_clients = int(max_clients)
        self.stream_limit = int(stream_limit)
        self._streams = {}
        self._placeholder_cache = {}
        self.encoded_frames = 0

    # ---------- параметры ----------
    def configure(self, jpeg_quality=None, stream_fps=None, stale_after=None,
                  command_stale_after=None, stream_limit=None):
        with self._lock:
            if jpeg_quality is not None:
                self.jpeg_quality = max(25, min(95, int(jpeg_quality)))
            if stream_fps is not None:
                self.stream_fps = max(1.0, min(60.0, float(stream_fps)))
            if stale_after is not None:
                self.stale_after = max(1.0, float(stale_after))
                # окно доступности команд не может быть уже порога «в эфире»
                self.command_stale_after = max(self.command_stale_after, self.stale_after)
            if command_stale_after is not None:
                self.command_stale_after = max(self.stale_after, float(command_stale_after))
            if stream_limit is not None:
                self.stream_limit = max(1, int(stream_limit))

    # ---------- публикация ----------
    def _slot(self, client_id, create=True):
        slot = self._slots.get(client_id)
        if slot is None and create:
            if len(self._slots) >= self.max_clients:
                log.warning("Достигнут лимит клиентов (%d), кадр от %s отброшен",
                            self.max_clients, client_id)
                return None
            slot = ClientSlot(client_id, self._cond)
            self._slots[client_id] = slot
            log.info("Новый клиент в трансляции: %s", client_id)
        return slot

    def publish_raw(self, client_id, frame, meta=None):
        """Кадр прямо с камеры (без аннотаций). Кодируется не чаще stream_fps."""
        now = time.time()
        with self._cond:
            slot = self._slot(client_id)
            if slot is None:
                return 0
            h, w = frame.shape[:2]
            slot.width, slot.height = int(w), int(h)
            slot.frames += 1
            prev_raw = slot.last_raw
            slot.last_raw = now
            # сырой кадр храним всегда (нужен для «добавить лицо из кадра»)
            slot.raw_frame = frame
            if meta:
                slot.meta = {**slot.meta, **meta}
            # EMA частоты кадров
            dt = now - prev_raw if prev_raw else 0.0
            if dt > 0:
                inst = min(1.0 / dt, 120.0)
                slot.fps = inst if slot.fps <= 0 else round(0.85 * slot.fps + 0.15 * inst, 2)
            min_interval = 1.0 / max(self.stream_fps, 0.5)
            if slot.raw_jpeg is not None and (now - slot.last_encode) < min_interval:
                return slot.seq_raw
            quality = self.jpeg_quality
        data = encode_image(frame, ".jpg", quality)
        with self._cond:
            slot = self._slots.get(client_id)
            if slot is None:
                return 0
            if data is not None:
                slot.raw_jpeg = data
                slot.seq_raw += 1
                slot.last_encode = now
                self.encoded_frames += 1
                self._cond.notify_all()
            return slot.seq_raw

    def publish_annotation_result(self, client_id, annotated_frame, annotations,
                                  faces=0, recognized=0, unknown=0):
        """Результат распознавания: кадр с рамками (для снимков) + координаты для браузера."""
        with self._cond:
            slot = self._slot(client_id)
            if slot is None:
                return
            slot.annotated_frame = annotated_frame
            slot.annotations = list(annotations or [])
            slot.faces = int(faces)
            slot.recognized = int(recognized)
            slot.unknown = int(unknown)
            slot.last_ann = time.time()
            slot.seq_ann += 1
            self._cond.notify_all()

    # ---------- чтение ----------
    def wait_raw(self, client_id, since_seq=0, timeout=1.0):
        """Ждёт кадр новее ``since_seq``. Возвращает (jpeg_bytes|None, seq)."""
        deadline = time.time() + timeout
        with self._cond:
            while True:
                slot = self._slots.get(client_id)
                if slot is not None and slot.seq_raw > since_seq and slot.raw_jpeg:
                    return slot.raw_jpeg, slot.seq_raw
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None, (slot.seq_raw if slot else since_seq)
                self._cond.wait(remaining)

    def latest_raw(self, client_id):
        """Последний закодированный JPEG клиента: (bytes|None, seq)."""
        with self._lock:
            slot = self._slots.get(client_id)
            return (slot.raw_jpeg, slot.seq_raw) if slot else (None, 0)

    def latest_raw_frame(self, client_id):
        """Последний сырой кадр клиента (numpy BGR) или None.

        Используется веб-панелью: список лиц в кадре, кроп лица для добавления
        нового человека прямо из текущего изображения.

        Возвращается сам массив (без копирования): поток приёма видео каждый раз
        **заменяет** ссылку на новый декодированный кадр и никогда не меняет
        содержимое старого, поэтому держать ссылку безопасно. Поток
        распознавания рисует разметку на своей копии (``frame.copy()``).
        """
        with self._lock:
            slot = self._slots.get(client_id)
            return slot.raw_frame if slot is not None else None

    def latest_annotated_jpeg(self, client_id, quality=None):
        """JPEG последнего аннотированного кадра (кодируется по требованию)."""
        with self._lock:
            slot = self._slots.get(client_id)
            frame = slot.annotated_frame if slot else None
            quality = int(quality or self.jpeg_quality)
        if frame is None:
            return None
        return encode_image(frame, ".jpg", quality)

    def clients(self):
        with self._lock:
            return sorted(self._slots.keys())

    def clients_info(self):
        now = time.time()
        with self._lock:
            slots = list(self._slots.values())
            stale = self.stale_after
        return sorted((s.public_info(stale, now) for s in slots),
                      key=lambda i: (not i["online"], i["client_id"]))

    def client_info(self, client_id):
        with self._lock:
            slot = self._slots.get(client_id)
            stale = self.stale_after
            return slot.public_info(stale) if slot else None

    def online_clients(self, stale_after=None):
        """Клиенты, от которых недавно приходило видео.

        ``stale_after=None`` → значение из настроек (индикация «в эфире»).
        """
        now = time.time()
        with self._lock:
            stale = self.stale_after if stale_after is None else float(stale_after)
            return [cid for cid, s in self._slots.items()
                    if s.last_raw and (now - s.last_raw) <= stale]

    def command_targets(self, client_id=None, stale_after=None):
        """Адресаты для команд панели (объявление, саундборд, привет).

        Отличается от :meth:`online_clients` более мягким таймаутом
        (:attr:`command_stale_after`): команды уходят по ZeroMQ PUB и не требуют
        свежего видео, а жёсткий 8-секундный порог приводил к ошибке
        «Нет клиентов в эфире» (409) при кратковременной потере кадров.

        Если в «мягком» окне никого нет, но клиенты в хабе известны — команда
        отправляется всем известным (PUB всё равно delivers только подписчикам).
        """
        if client_id:
            return [client_id]
        now = time.time()
        with self._lock:
            stale = (self.command_stale_after if stale_after is None
                     else float(stale_after))
            recent = [cid for cid, s in self._slots.items()
                      if s.last_raw and (now - s.last_raw) <= stale]
            if recent:
                return sorted(recent)
            return sorted(self._slots.keys())

    def forget(self, client_id):
        with self._cond:
            existed = self._slots.pop(client_id, None) is not None
            self._cond.notify_all()
        return existed

    def totals(self):
        with self._lock:
            slots = list(self._slots.values())
            stale = self.stale_after
            now = time.time()
            streams = sum(self._streams.values())
        online = [s for s in slots if s.last_raw and now - s.last_raw <= stale]
        return {
            "clients_total": len(slots),
            "clients_online": len(online),
            "frames_received": sum(s.frames for s in slots),
            "encoded_frames": self.encoded_frames,
            # сколько лиц видно ПРЯМО СЕЙЧАС (только клиенты в эфире);
            # "faces" оставлен для совместимости — сумма по всем слотам
            "current_faces": sum(s.faces for s in online),
            "faces": sum(s.faces for s in slots),
            "active_streams": streams,
            "stream_limit": self.stream_limit,
        }

    # ---------- учёт MJPEG-потоков ----------
    def register_stream(self, client_id):
        """Занимает слот потока. False — если достигнут лимит одновременных потоков."""
        with self._lock:
            current = sum(self._streams.values())
            if current >= self.stream_limit:
                log.warning("Лимит одновременных MJPEG-потоков (%d) достигнут, "
                            "поток для %s отклонён", self.stream_limit, client_id)
                return False
            self._streams[client_id] = self._streams.get(client_id, 0) + 1
            return True

    def release_stream(self, client_id):
        with self._lock:
            left = self._streams.get(client_id, 0) - 1
            if left <= 0:
                self._streams.pop(client_id, None)
            else:
                self._streams[client_id] = left

    def stream_stats(self):
        with self._lock:
            return {"active": sum(self._streams.values()),
                    "limit": self.stream_limit,
                    "by_client": dict(self._streams)}

    # ---------- снимки ----------
    def save_snapshot(self, client_id, directory="debug_frames", annotated=True,
                      prefix="frame"):
        """Сохраняет текущий кадр клиента на диск. Возвращает путь или None.

        Запись кроссплатформенная: ``cv2.imencode`` + байты (см.
        :mod:`facegate.imaging`). ``cv2.imwrite`` на Windows молча не создаёт
        файл, если в пути есть кириллица, — поэтому имя клиента приводится к
        ASCII, а результат записи проверяется.
        """
        with self._lock:
            slot = self._slots.get(client_id)
            frame = slot.annotated_frame.copy() if (slot and annotated
                                                   and slot.annotated_frame is not None) else None
            raw = slot.raw_jpeg if slot else None
            counter = slot.frames if slot else 0
        if frame is None and raw is None:
            log.warning("Снимок не сохранён: нет кадра от клиента %s", client_id)
            return None
        ok, err = ensure_directory(directory)
        if not ok:
            log.error("Снимок не сохранён: %s", err)
            return None
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe_id = ascii_safe_name(client_id, max_len=40)
        fname = f"{prefix}_{safe_id}_{stamp}_{counter:06d}.jpg"
        path = os.path.join(directory, fname)
        if frame is not None:
            ok, message = write_image(path, frame, quality=self.jpeg_quality)
        else:
            ok, message = write_bytes(path, raw)
        if not ok:
            log.error("Снимок не сохранён: %s", message)
            return None
        log.info("Снимок сохранён: %s", path)
        return path

    def list_snapshots(self, directory="debug_frames", limit=100):
        try:
            names = [n for n in os.listdir(directory) if n.lower().endswith((".jpg", ".png"))]
        except OSError:
            return []
        names.sort(reverse=True)
        out = []
        for name in names[:limit]:
            path = os.path.join(directory, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append({"name": name, "size": st.st_size, "mtime": st.st_mtime})
        return out

    # ---------- заглушка «нет сигнала» ----------
    def placeholder_jpeg(self, client_id, reason="NO SIGNAL"):
        key = (client_id, reason)
        cached = self._placeholder_cache.get(key)
        if cached and cached[1] > time.time() - 2.0:
            return cached[0]
        w, h = _PLACEHOLDER_SIZE
        img = np.full((h, w, 3), 22, dtype=np.uint8)
        cv2.rectangle(img, (0, 0), (w - 1, h - 1), (60, 66, 84), 2)
        cv2.putText(img, reason, (w // 2 - 110, h // 2 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (150, 158, 178), 2)
        label = translit(client_id)[:40]
        cv2.putText(img, label, (w // 2 - min(150, 8 * len(label) // 2), h // 2 + 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (110, 118, 138), 1)
        info = self.client_info(client_id)
        if info and info.get("last_frame_age") is not None:
            age = f"last frame {info['last_frame_age']:.0f}s ago"
            cv2.putText(img, age, (w // 2 - 110, h // 2 + 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (90, 96, 112), 1)
        data = encode_image(img, ".jpg", 60) or blank_jpeg()
        self._placeholder_cache[key] = (data, time.time())
        return data


def _multipart_part(boundary, data):
    return (b"--" + boundary + b"\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
            + data + b"\r\n")


def mjpeg_generator(hub, client_id, boundary=b"frame", idle_timeout=1.0,
                    gone_timeout=STREAM_GONE_TIMEOUT):
    """Генератор multipart/x-mixed-replace для Flask Response.

    Корректно завершается при закрытии соединения браузером: ``GeneratorExit``
    перехватывается, а слот потока освобождается в ``finally``. Без этого
    генератор продолжал работать после закрытия вкладки, удерживая поток
    Werkzeug, — со временем все воркеты панели оказывались заняты «мёртвыми»
    трансляциями и кнопки переставали отвечать.
    """
    if not hub.register_stream(client_id):
        try:
            yield _multipart_part(boundary,
                                  hub.placeholder_jpeg(client_id, "TOO MANY STREAMS"))
        except GeneratorExit:  # pragma: no cover
            return
        return
    started = time.time()
    sent = 0
    gone_since = None
    log.debug("MJPEG-поток открыт: %s (всего %d)", client_id,
              hub.stream_stats()["active"])
    try:
        seq = 0
        while True:
            data, new_seq = hub.wait_raw(client_id, seq, timeout=idle_timeout)
            if data is None:
                # клиента больше нет в хабе (убран вручную/забыт) — не висим вечно
                if hub.client_info(client_id) is None:
                    now = time.time()
                    gone_since = now if gone_since is None else gone_since
                    if gone_timeout and now - gone_since > gone_timeout:
                        log.info("MJPEG-поток %s закрыт: клиент отсутствует %.0f с",
                                 client_id, now - gone_since)
                        break
                else:
                    gone_since = None
                data = hub.placeholder_jpeg(client_id)
            else:
                seq = new_seq
                gone_since = None
            yield _multipart_part(boundary, data)
            sent += 1
    except GeneratorExit:
        # Браузер закрыл вкладку/соединение — штатный выход, не ошибка.
        log.debug("MJPEG-поток %s закрыт клиентом (отправлено %d кадров, %.1f с)",
                  client_id, sent, time.time() - started)
        raise
    except Exception as e:  # pragma: no cover - BrokenPipeError и прочее
        log.debug("MJPEG-поток %s завершён: %s", client_id, e)
    finally:
        hub.release_stream(client_id)
