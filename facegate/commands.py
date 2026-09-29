# -*- coding: utf-8 -*-
"""Отправка команд клиентам (ZeroMQ PUB) из одного выделенного потока.

Сокет ZeroMQ нельзя дёргать из разных потоков, а команды отправляются и из
потока распознавания, и из воркеров Flask (кнопки админ-панели). Поэтому
сокет живёт в своём потоке (:class:`CommandSender`), а наружу торчит
потокобезопасная очередь.

Форматы пакетов (:func:`build_greet_command`, :func:`build_speak_command`):
    {"action": "greet", "name": ..., "text": ..., "audio_b64": ...|null}
    {"action": "speak", "text": ..., "audio_b64": ...|null, "source": ...}
``audio_b64`` — готовый WAV (base64), клиент проигрывает его системным плеером.
"""
import base64
import logging
import queue
import threading
import time

import zmq

log = logging.getLogger(__name__)

SENTINEL = object()


def send_command(command_socket, client_id, payload):
    """Отправляет одну команду в уже существующий PUB-сокет."""
    command_socket.send_string(client_id, flags=zmq.SNDMORE)
    command_socket.send_json(payload)


DEFAULT_GREET_TEMPLATE = "Здравствуйте, {name}!"


def build_greet_command(name, wav_bytes, template=None, text=None):
    """Пакет greet: текст + (опционально) base64 WAV.

    ``text`` — уже подставленная фраза (нужен для вариативных приветствий:
    сервер выбирает случайный вариант из пула, и в команду/журнал должен попасть
    именно прозвучавший текст). Если ``text`` не задан, фраза формируется из
    ``template`` (обратная совместимость).
    """
    from facegate.tts.cache import format_template

    phrase = text if text else format_template(template or DEFAULT_GREET_TEMPLATE, name)
    payload = {
        "action": "greet",
        "name": name,
        "text": phrase,
        "audio_b64": None,
    }
    if wav_bytes:
        payload["audio_b64"] = base64.b64encode(wav_bytes).decode("ascii")
    return payload


def build_speak_command(text, wav_bytes=None, source="admin"):
    """Пакет speak: произвольное объявление (или звук с саундборда) для клиента."""
    payload = {"action": "speak", "text": text, "audio_b64": None, "source": source}
    if wav_bytes:
        payload["audio_b64"] = base64.b64encode(wav_bytes).decode("ascii")
    return payload


class CommandSender:
    """Очередь команд + поток-владелец PUB-сокета."""

    def __init__(self, port=5556, host="*", maxsize=1000, context=None):
        self.port = int(port)
        self.host = host
        self.bound_port = self.port
        self._queue = queue.Queue(maxsize=maxsize)
        self._context = context
        self._socket = None
        self._thread = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self.sent = 0
        self.dropped = 0
        self.last_error = None

    # ---------- жизненный цикл ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._loop, name="command-sender", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self

    def stop(self, timeout=2.0):
        self._stopped.set()
        try:
            self._queue.put_nowait(SENTINEL)
        except queue.Full:  # pragma: no cover
            pass
        if self._thread:
            self._thread.join(timeout=timeout)

    def ready(self):
        return self._ready.is_set()

    # ---------- отправка ----------
    def send(self, client_id, payload):
        """Ставит команду в очередь. Возвращает True, если принята."""
        if self._stopped.is_set():
            return False
        try:
            self._queue.put_nowait((client_id, payload))
            return True
        except queue.Full:
            self.dropped += 1
            log.warning("Очередь команд переполнена, команда для %s отброшена", client_id)
            return False

    def broadcast(self, payload, client_ids):
        sent = 0
        for client_id in client_ids:
            if self.send(client_id, payload):
                sent += 1
        return sent

    @property
    def pending(self):
        return self._queue.qsize()

    def stats(self):
        return {"sent": self.sent, "dropped": self.dropped,
                "pending": self.pending, "ready": self.ready(),
                "port": self.bound_port, "last_error": self.last_error}

    # ---------- внутренний поток ----------
    def _loop(self):
        context = self._context or zmq.Context()
        socket = context.socket(zmq.PUB)
        try:
            socket.setsockopt(zmq.SNDHWM, 500)
            socket.setsockopt(zmq.LINGER, 0)
            if self.port:
                socket.bind(f"tcp://{self.host}:{self.port}")
                self.bound_port = self.port
            else:
                # порт 0 = выбрать свободный (удобно в тестах)
                self.bound_port = socket.bind_to_random_port(f"tcp://{self.host}")
            self._socket = socket
            self._ready.set()
            log.info("PUB-сокет команд готов: tcp://%s:%s", self.host, self.bound_port)
        except zmq.ZMQError as e:
            self.last_error = str(e)
            log.error("Не удалось поднять PUB-сокет на порту %s: %s", self.port, e)
            self._ready.set()
            return

        while not self._stopped.is_set():
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is SENTINEL:
                break
            client_id, payload = item
            try:
                send_command(socket, client_id, payload)
                self.sent += 1
            except zmq.ZMQError as e:
                self.last_error = str(e)
                log.error("Ошибка отправки команды клиенту %s: %s", client_id, e)
        try:
            socket.close(0)
        except Exception:  # pragma: no cover
            pass
        if self._context is None:
            context.term()
        self._socket = None


def wait_for_clients(sender, timeout=1.0):
    """Даёт подписчикам время подключиться (slow joiner) перед массовой рассылкой."""
    time.sleep(min(max(timeout, 0.0), 5.0))
