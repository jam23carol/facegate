# -*- coding: utf-8 -*-
"""Дымовой прогон настоящего процесса сервера (end-to-end, без камеры).

    python tools/smoke_check.py            # поднять сервер, прогнать сценарий, остановить
    python tools/smoke_check.py --keep-logs

Сервер запускается реальным процессом (те же потоки, сокеты и HTTP), поэтому
прогон ловит проблемы, которые не видны в юнит-тестах: зависания HTTP-воркеров,
утечки MJPEG-потоков, молчаливое отсутствие файлов на диске.

Проверяет, что:
  * сервер стартует и отвечает по HTTP;
  * кадр от клиента виден в панели;
  * объявление не виснет и даёт осмысленный ответ;
  * «добавить лицо из кадра» возвращает ошибку, а НЕ вешает процесс;
  * MJPEG-поток освобождает слот после закрытия соединения;
  * остановка по SIGTERM проходит быстро.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import cv2
import numpy as np
import zmq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url, timeout=5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def post(url, payload, timeout=25.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def wait_for(fn, timeout=25.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


def main():
    tmp = tempfile.mkdtemp(prefix="smoke_")
    video_port, command_port, web_port = free_port(), free_port(), free_port()
    env = dict(os.environ, PYTHONPATH=ROOT, OMP_NUM_THREADS="1")
    proc = subprocess.Popen(
        [sys.executable, "-u", "server.py",
         "--no-auth", "--no-recognition",
         "--video-port", str(video_port),
         "--command-port", str(command_port),
         "--web-port", str(web_port),
         "--known-faces-dir", os.path.join(tmp, "faces"),
         "--voice-cache-dir", os.path.join(tmp, "voice"),
         "--admin-data-dir", os.path.join(tmp, "admin"),
         "--debug-dir", os.path.join(tmp, "frames"),
         "--log-level", "INFO", "--stats-interval", "0"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    failures = []

    def check(name, condition, detail=""):
        print(f"[{'OK ' if condition else 'FAIL'}] {name} {detail}")
        if not condition:
            failures.append(name)

    try:
        base = f"http://127.0.0.1:{web_port}"
        check("сервер поднялся", wait_for(lambda: _safe_health(base)) is not None)

        # --- кадр от клиента ---
        ctx = zmq.Context()
        push = ctx.socket(zmq.PUSH)
        push.setsockopt(zmq.LINGER, 0)
        push.connect(f"tcp://127.0.0.1:{video_port}")
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.connect(f"tcp://127.0.0.1:{command_port}")
        sub.setsockopt_string(zmq.SUBSCRIBE, "smoke-cam")
        sub.setsockopt(zmq.RCVTIMEO, 300)
        time.sleep(0.4)

        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        frame[:, :] = (40, 90, 160)
        cv2.rectangle(frame, (80, 60), (200, 180), (250, 250, 250), -1)
        ok, jpeg = cv2.imencode(".jpg", frame)
        for _ in range(3):
            push.send_string("smoke-cam", flags=zmq.SNDMORE)
            push.send(jpeg.tobytes())
            time.sleep(0.05)

        info = wait_for(lambda: _camera(base, "smoke-cam"))
        check("кадр виден в панели", bool(info and info.get("frames", 0) >= 1),
              f"frames={info and info.get('frames')}")

        # --- объявление (TTS в песочнице нет → честный ответ, без зависания) ---
        started = time.time()
        code, body = post(base + "/api/announce", {"text": "Проверка объявления"})
        elapsed = time.time() - started
        check("объявление не виснет", elapsed < 20.0, f"{elapsed:.1f} c")
        check("объявление принято", code == 200, f"code={code} body={body}")
        check("ответ содержит статус озвучки",
              body.get("voice") in ("ready", "pending", "unavailable"), str(body))

        got = None
        deadline = time.time() + 6
        while time.time() < deadline and got is None:
            try:
                sub.recv_string()
                got = json.loads(sub.recv_string())
            except zmq.Again:
                continue
        check("команда дошла до клиента", bool(got), str(got)[:80])
        if got:
            check("в команде есть текст", got.get("text") == "Проверка объявления")

        # --- добавление лица из кадра: ошибка, но процесс жив ---
        started = time.time()
        code, body = post(base + "/api/persons/from-frame",
                          {"client_id": "smoke-cam", "name": "Гость",
                           "left": 80, "top": 60, "right": 200, "bottom": 180})
        elapsed = time.time() - started
        check("from-frame не виснет", elapsed < 20.0, f"{elapsed:.1f} c")
        check("from-frame вернул понятную ошибку", code in (422, 503), f"code={code}")
        check("сервер жив после from-frame", _safe_health(base) is not None)

        # --- MJPEG: слот потока освобождается после закрытия ---
        stream_url = f"{base}/stream/smoke-cam"
        resp = urllib.request.urlopen(stream_url, timeout=5)
        chunk = resp.read(2048)
        check("MJPEG отдаёт данные", chunk.startswith(b"--faceframe"), str(chunk[:24]))
        status = json.loads(get(base + "/api/status")[1])
        check("поток учтён", status.get("active_streams", 0) >= 1,
              f"active_streams={status.get('active_streams')}")
        resp.close()
        time.sleep(1.0)
        released = wait_for(lambda: True if _active_streams(base) == 0 else None,
                            timeout=10.0)
        check("слот MJPEG освобождён после закрытия вкладки", bool(released),
              f"active={_active_streams(base)}")

        # --- снимок ---
        code, body = post(base + "/api/cameras/smoke-cam/snapshot", {})
        check("снимок сохранён", code == 200 and body.get("ok"), str(body)[:120])
        if body.get("path"):
            check("файл снимка существует", os.path.exists(body["path"]), body["path"])
            check("имя снимка ASCII", os.path.basename(body["path"]).isascii())

        # --- настройки панели ---
        _code, raw = get(base + "/api/settings")
        desc = json.loads(raw)
        check("пул фраз в настройках", "\n" in desc["settings"]["greet_templates"])
        check("устаревший шаблон скрыт", desc["spec"]["greet_template"]["hidden"] is True)

        # --- страница панели ---
        _code, html = get(base + "/")
        text = html.decode("utf-8")
        check("страница панели отдаётся", "Распознавание лиц" in text)
        check("в панели есть диалог объявления", 'id="announce-dialog"' in text)
        check("в панели есть download у ссылки", "data-act=\"dl\" download" in text)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            out = proc.communicate(timeout=15.0)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]
            failures.append("сервер не остановился по SIGTERM")
        tail = "\n".join((out or "").splitlines()[-12:])
        print("\n--- последние строки лога сервера ---")
        print(tail)
        check("сервер остановился", proc.returncode is not None,
              f"rc={proc.returncode}")
        check("в логе нет трейсбеков", "Traceback" not in (out or ""))

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
        return 1
    print("Дымовой прогон: OK")
    return 0


def _safe_health(base):
    try:
        code, body = get(base + "/api/health", timeout=2.0)
        return json.loads(body) if code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def _camera(base, client_id):
    try:
        code, body = get(f"{base}/api/cameras/{client_id}", timeout=2.0)
        return json.loads(body) if code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def _active_streams(base):
    try:
        _code, body = get(base + "/api/status", timeout=2.0)
        return json.loads(body)["active_streams"]
    except Exception:  # noqa: BLE001
        return -1


if __name__ == "__main__":
    sys.exit(main())
