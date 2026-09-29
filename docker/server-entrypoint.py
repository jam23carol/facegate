#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Точка входа контейнера сервера: права на данные + запуск от обычного пользователя.

Проблема: bind-тома (known_faces/, voice_cache/, admin_data/, debug_frames/,
soundboard/) при первом старте создаются Docker'ом от root, и сервер, запущенный
от root, сохраняет все файлы (озвучку, снимки, звуки, настройки) владельцем root
— на хосте их потом не отредактировать/не удалить без sudo.

Решение (как в linuxserver-образах): контейнер стартует от root, этот скрипт
  1) создаёт каталоги данных и делает их владельцем ${PUID}:${PGID};
  2) сбрасывает привилегии процесса до ${PUID}:${PGID} (по умолчанию 1000:1000);
  3) запускает основную команду (CMD).

Всё, что сервер сохраняет после этого, принадлежит вашему пользователю хоста.
PUID=0 оставляет запуск от root (не рекомендуется).
"""
import os
import sys

DATA_DIRS = [
    "/app/known_faces",
    "/app/voice_cache",
    "/app/admin_data",
    "/app/debug_frames",
    "/app/soundboard",
]


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        print(f"[entrypoint] {name}={os.environ.get(name)!r} — не число, беру {default}",
              flush=True)
        return default


def chown_tree(path, uid, gid):
    try:
        os.chown(path, uid, gid)
    except OSError as e:
        print(f"[entrypoint] WARN: chown {path}: {e}", flush=True)
        return
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chown(os.path.join(root, name), uid, gid)
            except OSError:
                pass


def main():
    uid = _env_int("PUID", 1000)
    gid = _env_int("PGID", 1000)

    if os.geteuid() == 0:
        for d in DATA_DIRS:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                print(f"[entrypoint] WARN: {d}: {e}", flush=True)
                continue
            chown_tree(d, uid, gid)
        if uid != 0:
            try:
                os.setgroups([gid])
            except OSError:
                pass
            try:
                os.setgid(gid)
                os.setuid(uid)
            except OSError as e:
                print(f"[entrypoint] WARN: не удалось сбросить привилегии до "
                      f"{uid}:{gid}: {e} — продолжаю от root", flush=True)
            else:
                os.environ["HOME"] = "/tmp"
                print(f"[entrypoint] данные принадлежат {uid}:{gid}, "
                      f"процесс запущен от {uid}:{gid}", flush=True)

    # Пробная запись в каждый каталог данных: если том примонтирован от root
    # (или PUID/PGID не совпали), сервер молча не сохранял фото/снимки/озвучку.
    # Здесь это видно сразу и с точной командой для починки.
    for d in DATA_DIRS:
        probe = os.path.join(d, ".write_probe")
        try:
            with open(probe, "wb") as f:
                f.write(b"ok")
            os.remove(probe)
        except OSError as e:
            print(f"[entrypoint] ОШИБКА: {d} недоступен для записи ({e}).",
                  file=sys.stderr, flush=True)
            print(f"[entrypoint]   Исправьте на хосте: sudo chown -R {uid}:{gid} {d}",
                  file=sys.stderr, flush=True)
            print("[entrypoint]   Сервер запустится, но фото/снимки/озвучка "
                  "сохраняться не будут.", file=sys.stderr, flush=True)

    if len(sys.argv) < 2:
        print("[entrypoint] команда запуска не передана", file=sys.stderr)
        return 2
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
