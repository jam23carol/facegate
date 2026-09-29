# -*- coding: utf-8 -*-
"""Журнал для админ-панели: кольцо событий и перехват логов сервера.

* :class:`EventBus`       — последние N событий распознавания/панели
                            (с монотонными номерами для инкрементального опроса);
* :class:`LogRingHandler` — ``logging.Handler``, дублирующий лог сервера
                            в такое же кольцо, чтобы журнал был виден в браузере.
"""
import logging
import threading
import time
from collections import deque


class EventBus:
    """Последние N событий с монотонными номерами (для инкрементального опроса)."""

    def __init__(self, maxlen=500):
        self._lock = threading.Lock()
        self._items = deque(maxlen=maxlen)
        self._seq = 0

    def push(self, kind, **fields):
        with self._lock:
            self._seq += 1
            item = {"seq": self._seq, "ts": time.time(), "type": kind}
            item.update(fields)
            self._items.append(item)
            return self._seq

    def since(self, seq=0, limit=200):
        with self._lock:
            items = [dict(i) for i in self._items if i["seq"] > seq]
        if len(items) > limit:
            items = items[-limit:]
        next_seq = items[-1]["seq"] if items else seq
        return items, next_seq

    def recent(self, limit=100):
        with self._lock:
            items = [dict(i) for i in self._items][-limit:]
        return items

    def __len__(self):
        with self._lock:
            return len(self._items)


class LogRingHandler(logging.Handler):
    """Пишет записи лога в кольцо, чтобы журнал был виден в браузере."""

    def __init__(self, maxlen=1000, level=logging.INFO):
        super().__init__(level=level)
        self._lock_buf = threading.Lock()
        self._items = deque(maxlen=maxlen)
        self._seq = 0
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            message = self.format(record)
            with self._lock_buf:
                self._seq += 1
                self._items.append({
                    "seq": self._seq,
                    "ts": record.created,
                    "level": record.levelname,
                    "name": record.name,
                    "message": message,
                })
        except Exception:  # pragma: no cover - логгер не должен ронять сервер
            self.handleError(record)

    def since(self, seq=0, limit=300):
        with self._lock_buf:
            items = [dict(i) for i in self._items if i["seq"] > seq]
        if len(items) > limit:
            items = items[-limit:]
        next_seq = items[-1]["seq"] if items else seq
        return items, next_seq

    def recent(self, limit=200):
        with self._lock_buf:
            return [dict(i) for i in self._items][-limit:]

    def attach(self, logger=None):
        target = logger or logging.getLogger()
        target.addHandler(self)
        return self
