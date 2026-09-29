# -*- coding: utf-8 -*-
"""Кроссплатформенная работа с изображениями (кодирование и запись на диск).

Зачем этот модуль существует
-----------------------------
``cv2.imwrite()`` в OpenCV **не умеет** работать с путями, содержащими символы
вне текущей ANSI-кодировки системы: на Windows путь с кириллицей
(``debug_frames/Камера_1_...jpg``) приводит к тому, что функция **молча**
возвращает ``False`` и файл не создаётся. Исключение не бросается, поэтому в
логе оставалось «Снимок сохранён», а на диске ничего не было.

Решение: всегда кодировать в память (``cv2.imencode``) и писать байты обычным
``open(path, "wb")`` — Python 3 работает с путями в Unicode корректно на всех
платформах. Дополнительно результат **проверяется** (файл существует и
непустой), а ошибки записи (в т.ч. ``PermissionError`` на bind-томах Docker)
превращаются в понятную диагностическую строку.

Помимо записи здесь живут утилиты имён файлов: :func:`translit` (cv2.putText
не умеет кириллицу) и :func:`ascii_safe_name` (гарантированно ASCII-имя файла
для снимков — чтобы путь не мог «сломаться» ни на одной ФС).
"""
import logging
import os

import cv2
import numpy as np

log = logging.getLogger(__name__)

DEFAULT_JPEG_QUALITY = 92

# ---------- транслитерация (для подписей на кадре и ASCII-имён файлов) ----------
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
    # украинские/белорусские буквы, которые иногда встречаются в именах камер
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "u",
}


def translit(text):
    """Кириллица → латиница (cv2.putText не умеет кириллицу)."""
    out = []
    for ch in str(text):
        low = ch.lower()
        if low in _TRANSLIT:
            out.append(_TRANSLIT[low].upper() if ch.isupper() else _TRANSLIT[low])
        elif ord(ch) < 128:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


def ascii_safe_name(text, max_len=40, fallback="unknown"):
    """Имя файла, состоящее только из ASCII-символов ``[A-Za-z0-9_.-]``.

    Русские имена камер транслитерируются (``Камера 1`` → ``Kamera_1``),
    остальные не-ASCII символы заменяются подчёркиванием. Это гарантирует,
    что снимок сохранится и в Windows, и в Docker-томе, и в архиве.
    """
    base = translit(text)
    cleaned = "".join(c if (c.isascii() and (c.isalnum() or c in "-_.")) else "_"
                      for c in base)
    cleaned = cleaned.replace("__", "_").strip("._ ")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_len].rstrip("._ ") or fallback


# ---------- кодирование ----------
def encode_image(image, ext=".jpg", quality=DEFAULT_JPEG_QUALITY):
    """Кодирует numpy-кадр в байты (JPEG/PNG). Возвращает bytes или None."""
    if image is None:
        return None
    ext = (ext or ".jpg").lower()
    params = []
    if ext in (".jpg", ".jpeg"):
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(max(1, min(100, quality)))]
    try:
        ok, buf = cv2.imencode(ext, image, params)
    except cv2.error as e:
        log.error("Не удалось закодировать изображение (%s): %s", ext, e)
        return None
    if not ok or buf is None:
        return None
    return buf.tobytes()


def encode_jpeg(image, quality=DEFAULT_JPEG_QUALITY):
    """JPEG-байты кадра или None (тонкая обёртка :func:`encode_image`)."""
    return encode_image(image, ".jpg", quality)


def decode_image(data, flags=cv2.IMREAD_COLOR):
    """Декодирует байты изображения в numpy-кадр (или None)."""
    if not data:
        return None
    try:
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), flags)
    except (cv2.error, ValueError) as e:
        log.error("Не удалось декодировать изображение: %s", e)
        return None


# ---------- запись на диск ----------
def ensure_directory(directory):
    """Создаёт каталог. Возвращает (ok, error_message|None)."""
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as e:
        return False, _describe_os_error(e, directory)
    if not os.access(directory, os.W_OK):
        return False, (f"Каталог {directory} недоступен для записи "
                       f"(владелец/права: {_owner_hint(directory)}). "
                       "В Linux/Docker выполните: sudo chown -R 1000:1000 "
                       f"{directory}  (или запустите контейнер с PUID/PGID вашего пользователя)")
    return True, None


def _owner_hint(path):
    try:
        st = os.stat(path)
        return f"uid={st.st_uid}, gid={st.st_gid}, mode={oct(st.st_mode)[-3:]}"
    except OSError:
        return "?"


def _describe_os_error(exc, path):
    if isinstance(exc, PermissionError):
        return (f"Нет прав на запись в {path} ({_owner_hint(os.path.dirname(path) or '.')}). "
                "Linux/Docker: sudo chown -R $UID:$GID <каталог> или PUID/PGID контейнера")
    return f"Ошибка записи {path}: {exc}"


def write_image(path, image, quality=DEFAULT_JPEG_QUALITY, ext=None):
    """Сохраняет кадр на диск кроссплатформенно. Возвращает (ok, message).

    * кодирование в память + запись байтов (пути с кириллицей работают везде);
    * проверка, что файл действительно появился и непустой;
    * понятная диагностика прав доступа (типичная проблема bind-томов).
    """
    if image is None:
        return False, "Пустой кадр"
    ext = ext or os.path.splitext(path)[1] or ".jpg"
    directory = os.path.dirname(os.path.abspath(path))
    ok, err = ensure_directory(directory)
    if not ok:
        return False, err
    data = encode_image(image, ext, quality)
    if data is None:
        return False, f"Не удалось закодировать изображение в {ext}"
    tmp = f"{path}.part"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, _describe_os_error(e, path)
    try:
        size = os.path.getsize(path)
    except OSError as e:  # pragma: no cover - экзотика ФС
        return False, _describe_os_error(e, path)
    if size <= 0:  # pragma: no cover - защита от «тихого» сбоя записи
        return False, f"Файл {path} создан, но пустой"
    return True, path


def write_bytes(path, data):
    """Пишет готовые байты (например, исходный JPEG кадра). (ok, message)."""
    if not data:
        return False, "Пустые данные"
    directory = os.path.dirname(os.path.abspath(path))
    ok, err = ensure_directory(directory)
    if not ok:
        return False, err
    tmp = f"{path}.part"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, _describe_os_error(e, path)
    if os.path.getsize(path) <= 0:  # pragma: no cover
        return False, f"Файл {path} создан, но пустой"
    return True, path
