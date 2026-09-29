# -*- coding: utf-8 -*-
"""Проверка полного цикла без камеры: отправляет изображение как кадр видео,
слушает команды сервера и (опционально) проверяет, что кадр виден в админ-панели.

Примеры:
    python tools/e2e_check.py --image known_faces/michael.jpg --name tester
    python tools/e2e_check.py --image photo.jpg --check-web --web-port 8080
"""
import argparse
import base64
import json
import threading
import time
import urllib.error
import urllib.request

import cv2
import zmq


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="E2E проверка сервера распознавания")
    p.add_argument("--server-ip", default="127.0.0.1")
    p.add_argument("--video-port", type=int, default=5555)
    p.add_argument("--command-port", type=int, default=5556)
    p.add_argument("--web-port", type=int, default=8080, help="Порт админ-панели")
    p.add_argument("--client-id", default="e2e-check")
    p.add_argument("--image", required=True, help="JPEG/PNG файл с лицом")
    p.add_argument("--frames", type=int, default=3)
    p.add_argument("--fps", type=float, default=4.0)
    p.add_argument("--timeout", type=float, default=25.0, help="Сколько ждать команду greet")
    p.add_argument("--check-web", action="store_true",
                   help="Проверить, что кадр попал в админ-панель (MJPEG/API)")
    p.add_argument("--web-user", default=None, help="Логин панели (если включена авторизация)")
    p.add_argument("--web-password", default=None, help="Пароль панели")
    return p.parse_args(argv)


def http_get(url, timeout=5.0, opener=None):
    opener = opener or urllib.request.build_opener()
    with opener.open(url, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def web_login(base, user, password):
    """Входит в админ-панель и возвращает opener с живой сессией (или None)."""
    import http.cookiejar
    import re
    import urllib.parse
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        _status, _ct, html = http_get(base + "/login", opener=opener)
    except (urllib.error.URLError, OSError) as e:
        print(f"[!] панель недоступна: {e}")
        return None
    page = html.decode("utf-8", "replace")
    m = re.search(r'name="csrf_token" value="([^"]+)"', page)
    if not m:
        print("[!] панель не требует входа или разметка изменилась")
        return opener
    data = urllib.parse.urlencode({"username": user or "admin",
                                   "password": password or "admin",
                                   "csrf_token": m.group(1), "next": "/"}).encode()
    try:
        opener.open(base + "/login", data=data, timeout=5.0).read()
    except urllib.error.HTTPError as e:
        print(f"[!] вход в панель не удался: HTTP {e.code}")
        return None
    return opener


def check_web(args, expect_client_id):
    base = f"http://{args.server_ip}:{args.web_port}"
    opener = None
    if args.web_user or args.web_password:
        opener = web_login(base, args.web_user, args.web_password)
        if opener is None:
            return False
    ok = True
    try:
        status, _ct, body = http_get(base + "/api/health", opener=opener)
        health = json.loads(body)
        print(f"[web] /api/health -> {status} {health}")
        ok = ok and status == 200 and health.get("status") == "ok"
    except Exception as e:  # noqa: BLE001
        print(f"[web] /api/health недоступен: {e}")
        return False

    try:
        status, _ct, body = http_get(base + "/api/cameras", opener=opener)
        cams = json.loads(body).get("cameras", [])
        ids = [c["client_id"] for c in cams]
        print(f"[web] /api/cameras -> {status}, клиенты: {ids}")
        found = any(c["client_id"] == expect_client_id and c.get("has_video") for c in cams)
        print(f"[web] клиент {expect_client_id} виден в панели: {found}")
        ok = ok and found
    except Exception as e:  # noqa: BLE001
        print(f"[web] /api/cameras ошибка: {e}")
        ok = False

    try:
        status, ctype, body = http_get(base + f"/api/cameras/{expect_client_id}/frame.jpg",
                                       opener=opener)
        is_jpeg = body[:2] == b"\xff\xd8"
        print(f"[web] снимок кадра -> {status} {ctype} {len(body)} байт, JPEG: {is_jpeg}")
        ok = ok and status == 200 and is_jpeg
    except Exception as e:  # noqa: BLE001
        print(f"[web] кадр из панели не получен: {e}")
        ok = False
    return ok


def main():
    args = parse_args()
    img = cv2.imread(args.image)
    if img is None:
        raise SystemExit(f"Не удалось прочитать {args.image}")
    h, w = img.shape[:2]
    if w > 800:
        scale = 800 / w
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise SystemExit("Не удалось закодировать кадр")

    ctx = zmq.Context()
    push = ctx.socket(zmq.PUSH)
    push.connect(f"tcp://{args.server_ip}:{args.video_port}")
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://{args.server_ip}:{args.command_port}")
    sub.setsockopt_string(zmq.SUBSCRIBE, args.client_id)

    got = {}

    def listener():
        while not got.get("stop"):
            if sub.poll(200):
                _topic = sub.recv_string()
                try:
                    msg = json.loads(sub.recv_string())
                except Exception as e:  # noqa: BLE001
                    print(f"[!] битый JSON: {e}")
                    continue
                audio_len = len(msg.get("audio_b64") or "")
                print(f"[<=] команда: action={msg.get('action')} name={msg.get('name')} "
                      f"text={msg.get('text')!r} audio_b64={audio_len} байт(base64)")
                if msg.get("action") == "greet":
                    got["msg"] = msg

    t = threading.Thread(target=listener, daemon=True)
    t.start()
    time.sleep(0.7)  # slow joiner

    interval = 1.0 / max(args.fps, 0.1)
    meta = {"client_id": args.client_id, "hostname": "e2e-check", "camera_id": None,
            "width": int(img.shape[1]), "height": int(img.shape[0]), "version": "e2e"}
    for i in range(args.frames):
        push.send_string(args.client_id, flags=zmq.SNDMORE)
        if i == 0:
            push.send(jpg.tobytes(), flags=zmq.SNDMORE)
            push.send_string(json.dumps(meta, ensure_ascii=False))
        else:
            push.send(jpg.tobytes())
        print(f"[=>] кадр {i + 1}/{args.frames} отправен ({len(jpg)} байт)")
        time.sleep(interval)

    deadline = time.time() + args.timeout
    while time.time() < deadline and "msg" not in got:
        time.sleep(0.1)
    got["stop"] = True

    web_ok = None
    if args.check_web:
        web_ok = check_web(args, args.client_id)

    exit_code = 0
    if "msg" in got:
        audio = got["msg"].get("audio_b64")
        print("OK: приветствие получено"
              + (f", аудио {len(audio)} b64-символов (~{len(audio) * 3 // 4} байт WAV)"
                 if audio else ", БЕЗ аудио"))
    else:
        print("FAIL: приветствие не получено за отведённое время "
              "(возможно, лицо не найдено или человек не добавлен в known_faces/)")
        exit_code = 1

    if web_ok is True:
        print("OK: кадр виден в админ-панели")
    elif web_ok is False:
        print("FAIL: проверка админ-панели не прошла")
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
