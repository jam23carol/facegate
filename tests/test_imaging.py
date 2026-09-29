# -*- coding: utf-8 -*-
"""Кроссплатформенная запись изображений.

Регрессия: «на сервере не сохранялись фото». ``cv2.imwrite`` в Windows молча
возвращает False на путях с не-ASCII символами, а код не проверял результат —
в лог писалось «Снимок сохранён», файла на диске не было. Теперь запись идёт
через ``imencode`` + байты, имя приводится к ASCII, результат проверяется,
а ошибки прав доступа возвращают понятный текст.
"""
import os

import cv2
import numpy as np
import pytest

from facegate import imaging


def frame(w=64, h=48, color=(30, 60, 90)):
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :] = color
    return img


def test_ascii_safe_name_transliterates_cyrillic():
    assert imaging.ascii_safe_name("Камера 1") == "Kamera_1"
    assert imaging.ascii_safe_name("Офис — вход/выход") .replace("_", "") != ""
    name = imaging.ascii_safe_name("Камера 1")
    assert name.isascii()
    assert all(c.isalnum() or c in "-_." for c in name)


def test_ascii_safe_name_fallback_and_limit():
    assert imaging.ascii_safe_name("///") == "unknown"
    assert imaging.ascii_safe_name("") == "unknown"
    assert len(imaging.ascii_safe_name("ы" * 200, max_len=12)) <= 12


def test_translit():
    assert imaging.translit("Михаил") == "Mihail"
    assert imaging.translit("John") == "John"


def test_write_image_creates_file_with_cyrillic_path(tmp_path):
    directory = tmp_path / "снимки" / "Камера №1"
    path = os.path.join(str(directory), "кадр_1.jpg")
    ok, message = imaging.write_image(path, frame())
    assert ok is True, message
    assert message == path
    assert os.path.exists(path) and os.path.getsize(path) > 0
    img = cv2.imread(path)
    assert img is not None and img.shape[:2] == (48, 64)


def test_write_image_verifies_result(tmp_path):
    path = str(tmp_path / "a.jpg")
    ok, _ = imaging.write_image(path, frame())
    assert ok and os.path.getsize(path) > 100


def test_write_image_rejects_none(tmp_path):
    ok, message = imaging.write_image(str(tmp_path / "x.jpg"), None)
    assert ok is False and "Пустой кадр" in message


def test_write_image_reports_permission_error(tmp_path, monkeypatch):
    path = str(tmp_path / "denied" / "x.jpg")

    def deny(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(imaging.os, "makedirs", deny)
    ok, message = imaging.write_image(path, frame())
    assert ok is False
    assert "прав" in message.lower() or "permission" in message.lower()


def test_write_image_uses_atomic_replace(tmp_path, monkeypatch):
    """Файл появляется только целиком: сначала .part, затем os.replace."""
    seen = []
    real_replace = os.replace

    def spy_replace(src, dst):
        seen.append((os.path.basename(src), os.path.basename(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(imaging.os, "replace", spy_replace)
    path = str(tmp_path / "atomic.jpg")
    ok, _ = imaging.write_image(path, frame())
    assert ok and seen and seen[0][0].endswith(".part")
    assert os.path.exists(path) and not os.path.exists(path + ".part")


def test_write_bytes_roundtrip(tmp_path):
    path = str(tmp_path / "raw" / "кадр.jpg")
    data = imaging.encode_image(frame(), ".jpg", 80)
    ok, message = imaging.write_bytes(path, data)
    assert ok is True
    with open(path, "rb") as f:
        assert f.read() == data


def test_encode_decode_roundtrip():
    data = imaging.encode_image(frame(color=(10, 200, 30)), ".png")
    assert data and data[:4] == b"\x89PNG"
    img = imaging.decode_image(data)
    assert img is not None and img.shape == (48, 64, 3)


def test_decode_image_garbage():
    assert imaging.decode_image(b"not an image") is None
    assert imaging.decode_image(b"") is None


def test_ensure_directory_writable(tmp_path):
    ok, err = imaging.ensure_directory(str(tmp_path / "new" / "dir"))
    assert ok is True and err is None
    assert (tmp_path / "new" / "dir").is_dir()


def test_ensure_directory_reports_unwritable(tmp_path, monkeypatch):
    target = str(tmp_path / "ro")
    os.makedirs(target)
    monkeypatch.setattr(imaging.os, "access", lambda *a, **kw: False)
    ok, err = imaging.ensure_directory(target)
    assert ok is False
    assert "недоступен для записи" in err
    assert "chown" in err          # подсказка для Docker/Bind-mount


@pytest.mark.parametrize("quality", (30, 92))
def test_jpeg_quality_applied(quality):
    data = imaging.encode_jpeg(frame(), quality=quality)
    assert data and data[:2] == b"\xff\xd8"
