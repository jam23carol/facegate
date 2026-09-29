# -*- coding: utf-8 -*-
import cv2
import numpy as np

from facegate.recognition.store import FaceStore, sanitize_name


def png_bytes(w=40, h=40):
    ok, buf = cv2.imencode(".png", np.zeros((h, w, 3), dtype=np.uint8))
    assert ok
    return buf.tobytes()


def test_sanitize_name():
    assert sanitize_name("  Иван   Петров ") == "Иван Петров"
    assert sanitize_name("../evil/name") == "evil name"
    assert sanitize_name('a<b>:"|?*\x00') == "a b"
    assert sanitize_name("") == ""
    assert len(sanitize_name("x" * 200)) == 64


def test_add_photo_creates_file_and_entry(fake_fr, tmp_path):
    store = FaceStore(str(tmp_path))
    ok, msg = store.add_photo("Михаил", png_bytes())
    assert ok is True
    assert msg == "Михаил.jpg"
    assert (tmp_path / "Михаил.jpg").exists()
    assert store.has("Михаил")
    assert store.photos("Михаил") == ["Михаил.jpg"]
    entries = store.entries()
    assert len(entries) == 1
    name, enc = entries[0]
    assert name == "Михаил"
    assert enc.shape == (128,)


def test_add_second_photo_gets_suffix(fake_fr, tmp_path):
    store = FaceStore(str(tmp_path))
    ok1, f1 = store.add_photo("Анна", png_bytes())
    ok2, f2 = store.add_photo("Анна", png_bytes())
    assert ok1 and ok2
    assert f1 == "Анна.jpg"
    assert f2 == "Анна_2.jpg"
    assert sorted(store.photos("Анна")) == ["Анна.jpg", "Анна_2.jpg"]
    assert len(store.entries()) == 2


def test_add_photo_without_face_rejected(fake_fr, tmp_path):
    fake_fr.faces_present = False
    store = FaceStore(str(tmp_path))
    ok, msg = store.add_photo("Боб", png_bytes())
    assert ok is False
    assert "лицо" in msg.lower()
    assert not list(tmp_path.iterdir())


def test_add_garbage_bytes_rejected(fake_fr, tmp_path):
    store = FaceStore(str(tmp_path))
    ok, msg = store.add_photo("Боб", b"\xff\xffnotanimage")
    assert ok is False
    assert "декодировать" in msg


def test_delete_person_removes_files(fake_fr, tmp_path):
    store = FaceStore(str(tmp_path))
    store.add_photo("Анна", png_bytes())
    store.add_photo("Анна", png_bytes())
    files = [tmp_path / f for f in ("Анна.jpg", "Анна_2.jpg")]
    assert all(f.exists() for f in files)
    assert store.delete_person("Анна") is True
    assert not store.has("Анна")
    assert not any(f.exists() for f in files)
    assert store.delete_person("Анна") is False


def test_load_groups_suffixes(fake_fr, tmp_path):
    for fname in ("michael.jpg", "michael_2.jpg", "anna.png", "readme.txt"):
        (tmp_path / fname).write_bytes(b"stub")
    store = FaceStore(str(tmp_path))
    persons = store.load()
    assert sorted(persons.keys()) == ["anna", "michael"]
    assert len(persons["michael"]) == 2
    assert store.names() == ["anna", "michael"]


def test_entries_snapshot_is_isolated(fake_fr, tmp_path):
    store = FaceStore(str(tmp_path))
    store.add_photo("Анна", png_bytes())
    snap = store.entries()
    snap[0] = ("Взломщик", snap[0][1])
    assert store.entries()[0][0] == "Анна"


# ---------------------------------------------------------------------------
# Регрессии: добавление лица из кадра панели и сохранение файлов
# ---------------------------------------------------------------------------
import pytest

from facegate.recognition import engine as recog


def jpeg_bytes(w=40, h=40):
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :] = (120, 90, 70)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def test_add_photo_with_embedding_skips_detection(fake_fr, tmp_path):
    """Плотный кроп лица + готовый эмбеддинг → фото сохраняется без детекции.

    Раньше add_photo повторно искал лицо на обрезке, HOG возвращал 0 лиц,
    и панель отвечала 422 «На фото не найдено лицо» — файл не сохранялся.
    """
    fake_fr.faces_present = False          # детектор на кропе ничего не найдёт
    store = FaceStore(str(tmp_path))
    enc = np.zeros(128, dtype=np.float32)
    enc[7] = 0.5
    ok, msg = store.add_photo("Иван Петров", jpeg_bytes(), embedding=enc)
    assert ok is True, msg
    assert msg == "Иван Петров.jpg"
    assert (tmp_path / "Иван Петров.jpg").exists()
    assert store.has("Иван Петров")
    name, stored_enc = store.entries()[0]
    assert name == "Иван Петров"
    assert float(stored_enc[7]) == pytest.approx(0.5)


class SizeSensitiveFake:
    """HOG «не видит» мелкий кроп, но находит лицо на варианте с полями."""

    def __init__(self, inner, min_side=60):
        self.inner = inner
        self.min_side = min_side
        self.calls = []

    def load_image_file(self, path):
        return self.inner.load_image_file(path)

    def face_locations(self, img, model="hog"):
        h, w = img.shape[:2]
        self.calls.append((w, h, model))
        if min(h, w) < self.min_side:
            return []
        return self.inner.face_locations(img, model)

    def face_encodings(self, img, boxes=None):
        return self.inner.face_encodings(img, boxes)

    def face_distance(self, encodings, face_to_compare):
        return self.inner.face_distance(encodings, face_to_compare)


def test_add_photo_crop_fallback_with_padding(fake_fr, tmp_path, monkeypatch):
    """Плотный кроп сохраняется: детекция повторяется на изображении с полями."""
    sized = SizeSensitiveFake(fake_fr, min_side=60)
    monkeypatch.setattr(recog, "face_recognition", sized)
    store = FaceStore(str(tmp_path))
    ok, msg = store.add_photo("Анна Кроп", jpeg_bytes(40, 40))
    assert ok is True, msg
    assert (tmp_path / "Анна Кроп.jpg").exists()
    # первая попытка — на исходном кропе (40px), затем — на варианте с полями
    assert sized.calls[0][0] < 60
    assert any(w >= 60 for w, _h, _m in sized.calls)


def test_add_photo_rejected_when_no_face_anywhere(fake_fr, tmp_path):
    fake_fr.faces_present = False
    store = FaceStore(str(tmp_path))
    ok, msg = store.add_photo("Боб", jpeg_bytes())
    assert ok is False
    assert "лицо" in msg.lower()
    assert not list(tmp_path.iterdir())
    assert not store.has("Боб")


def test_add_photo_reports_busy_engine(fake_fr, tmp_path, monkeypatch):
    """Движок занят потоком видео → понятная ошибка, а не зависание."""
    def busy(*args, **kwargs):
        raise recog.InferenceBusyError("Движок распознавания занят (test)")

    monkeypatch.setattr(recog, "detect_with_embeddings", busy)
    store = FaceStore(str(tmp_path))
    ok, msg = store.add_photo("Влад", jpeg_bytes())
    assert ok is False and "занят" in msg
    assert not store.has("Влад")


def test_add_photo_reports_write_error(fake_fr, tmp_path, monkeypatch):
    from facegate.recognition import store as store_mod

    monkeypatch.setattr(store_mod.imaging, "write_bytes",
                        lambda path, data: (False, "Нет прав на запись в known_faces/"))
    store = FaceStore(str(tmp_path))
    ok, msg = store.add_photo("Гарри", jpeg_bytes())
    assert ok is False and "прав" in msg
    assert not store.has("Гарри")      # в базе не появился «призрачный» эталон


def test_add_photo_saves_file_with_cyrillic_name(fake_fr, tmp_path):
    """cv2.imwrite молча не пишет пути с кириллицей — здесь запись через байты."""
    store = FaceStore(str(tmp_path / "лица"))
    ok, fname = store.add_photo("Михаил Ёжкин", jpeg_bytes())
    assert ok is True
    path = tmp_path / "лица" / fname
    assert path.exists() and path.stat().st_size > 0
    assert cv2.imread(str(path)) is not None


class TightCropLoadFake:
    """Эталон сохранён плотным кропом: обычный поиск лица на нём не работает."""

    def __init__(self, inner):
        self.inner = inner

    def load_image_file(self, path):
        self.inner.load_image_file(path)
        return np.zeros((30, 30, 3), dtype=np.uint8)     # «мелкий» кроп

    def face_locations(self, img, model="hog"):
        h, w = img.shape[:2]
        if min(h, w) < 60:
            return []
        return self.inner.face_locations(img, model)

    def face_encodings(self, img, boxes=None):
        if boxes is None and min(img.shape[:2]) < 60:
            return []                                    # как в реальности
        return self.inner.face_encodings(img, boxes)

    def face_distance(self, encodings, face_to_compare):
        return self.inner.face_distance(encodings, face_to_compare)


def test_load_recovers_tight_crop_references(fake_fr, tmp_path, monkeypatch):
    """После перезапуска сервера кроп-эталоны не теряются (эвристика полей)."""
    store = FaceStore(str(tmp_path))
    ok, fname = store.add_photo("Пётр", jpeg_bytes(40, 40),
                                embedding=np.zeros(128, dtype=np.float32))
    assert ok

    monkeypatch.setattr(recog, "face_recognition", TightCropLoadFake(fake_fr))
    fresh = FaceStore(str(tmp_path))
    persons = fresh.load()
    assert "Пётр" in persons and len(persons["Пётр"]) == 1
