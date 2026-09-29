# -*- coding: utf-8 -*-
"""Очередь команд и PUB-сокет (commands.py)."""
import base64
import json
import time

import pytest
import zmq

from facegate.commands import (CommandSender, build_greet_command, build_speak_command,
                      send_command)


@pytest.fixture
def subscriber():
    """SUB-сокет, подключённый к случайному порту CommandSender."""
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    made = []

    def connect(sender, topic="clientX"):
        sub.connect(f"tcp://127.0.0.1:{sender.bound_port}")
        sub.setsockopt_string(zmq.SUBSCRIBE, topic)
        made.append(topic)
        time.sleep(0.35)          # slow joiner
        return sub

    yield connect, sub
    sub.close(0)
    ctx.term()


def recv_cmd(sub, timeout=3000):
    if not sub.poll(timeout):
        return None, None
    topic = sub.recv_string()
    return topic, json.loads(sub.recv_string())


def test_sender_roundtrip(subscriber):
    connect, sub = subscriber
    sender = CommandSender(port=0).start()
    try:
        assert sender.ready() is True
        connect(sender)
        assert sender.send("clientX", {"action": "ping"}) is True
        topic, msg = recv_cmd(sub)
        assert topic == "clientX"
        assert msg["action"] == "ping"
        assert sender.stats()["sent"] == 1
    finally:
        sender.stop(timeout=2.0)


def test_sender_broadcast(subscriber):
    connect, sub = subscriber
    sender = CommandSender(port=0).start()
    try:
        connect(sender, topic="")           # пустой топик = слушать всех
        n = sender.broadcast({"action": "speak", "text": "всем привет"},
                             ["a", "b", "c"])
        assert n == 3
        got = sorted(recv_cmd(sub)[0] for _ in range(3))
        assert got == ["a", "b", "c"]
    finally:
        sender.stop(timeout=2.0)


def test_sender_stop_is_idempotent():
    sender = CommandSender(port=0).start()
    sender.stop(timeout=1.0)
    sender.stop(timeout=1.0)
    assert sender.send("x", {"action": "ping"}) is False   # после остановки не принимаем


def test_sender_queue_overflow_drops():
    sender = CommandSender(port=0, maxsize=2)   # поток не запускаем — очередь не разгребается
    assert sender.send("a", {"i": 1}) is True
    assert sender.send("a", {"i": 2}) is True
    assert sender.send("a", {"i": 3}) is False
    assert sender.stats()["dropped"] == 1


def test_build_greet_command():
    wav = b"RIFFxxxxWAVEdata"
    payload = build_greet_command("Михаил", wav, "Здравствуйте, {name}!")
    assert payload["action"] == "greet"
    assert payload["name"] == "Михаил"
    assert payload["text"] == "Здравствуйте, Михаил!"
    assert base64.b64decode(payload["audio_b64"]) == wav
    assert build_greet_command("Анна", None, "{name}")["audio_b64"] is None


def test_build_speak_command():
    payload = build_speak_command("Обед", b"RIFF", source="admin")
    assert payload["action"] == "speak"
    assert payload["text"] == "Обед"
    assert payload["source"] == "admin"
    assert base64.b64decode(payload["audio_b64"]) == b"RIFF"
    assert build_speak_command("текст")["audio_b64"] is None


def test_send_command_helper():
    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    port = pub.bind_to_random_port("tcp://127.0.0.1")
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://127.0.0.1:{port}")
    sub.setsockopt_string(zmq.SUBSCRIBE, "t1")
    try:
        time.sleep(0.35)
        send_command(pub, "t1", {"action": "greet", "name": "X"})
        topic, msg = recv_cmd(sub)
        assert topic == "t1" and msg["name"] == "X"
    finally:
        pub.close(0)
        sub.close(0)
        ctx.term()
