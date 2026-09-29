#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Точка входа сервера распознавания лиц.

Вся логика — в пакете :mod:`facegate` (см. facegate/server.py). Этот файл
существует только для удобства запуска::

    python server.py                       # видео :5555, команды :5556, панель :8080
    python server.py --help                # все параметры
    python server.py --tts-engine silero   # нейросетевая русская озвучка
"""
from facegate.server import main

if __name__ == "__main__":
    main()
