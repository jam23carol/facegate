# -*- coding: utf-8 -*-
"""FaceGate — система распознавания лиц с голосовым приветствием и админ-панелью.

Пакет разделён на независимые слои (каждый отвечает только за свою логику):

  * :mod:`facegate.config`       — настройки, которые меняются из админ-панели;
  * :mod:`facegate.stats`        — потокобезопасные счётчики;
  * :mod:`facegate.events`       — кольцо событий и перехват логов для панели;
  * :mod:`facegate.throttle`     — антиспам приветствий (кулдаун на человека);
  * :mod:`facegate.commands`     — отправка команд клиентам (ZeroMQ PUB);
  * :mod:`facegate.imaging`      — кроссплатформенное кодирование/запись кадров;
  * :mod:`facegate.frames`       — приём/хранение кадров, MJPEG-трансляция;
  * :mod:`facegate.recognition`  — поиск лиц, эмбеддинги, хранилище эталонов;
  * :mod:`facegate.tts`          — движки синтеза речи (Silero/espeak/pyttsx3)
                                   и кэш заранее синтезированных фраз;
  * :mod:`facegate.soundboard`   — библиотека звуков для вкладки «Саундборд»;
  * :mod:`facegate.web`          — админ-панель (Flask): страницы, HTTP API;
  * :mod:`facegate.server`       — сборка сервера: потоки приёма видео,
                                   распознавания, мониторинга и веб-панели.

Точки входа (корень проекта): ``server.py`` и ``client.py``.

Ограничение нативных потоков (важно!)
-------------------------------------
Переменные окружения OpenMP/MKL/OpenBLAS выставляются **до** импорта
numpy/cv2/dlib: многопоточный OpenMP внутри dlib при параллельных вызовах из
разных потоков Python — типовая причина полного зависания процесса. Значения
можно переопределить, задав переменные до запуска сервера.
"""
import os as _os

# Один поток OpenMP для dlib/face_recognition: детекция остаётся
# предсказуемой, а свободные ядра достаются веб-панели, TTS и ZeroMQ.
for _var, _default in (
        ("OMP_NUM_THREADS", "1"),
        ("OPENBLAS_NUM_THREADS", "1"),
        ("MKL_NUM_THREADS", "1"),
        ("NUMEXPR_NUM_THREADS", "1"),
        ("VECLIB_MAXIMUM_THREADS", "1"),
        # воркер Silero TTS ограничен отдельно (SILERO_THREADS, см. silero_worker)
        ("SILERO_THREADS", "2"),
):
    _os.environ.setdefault(_var, _default)

VERSION = "2.3.1"
