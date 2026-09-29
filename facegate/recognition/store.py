# -*- coding: utf-8 -*-
"""Хранилище эталонов лиц: имя человека → [(файл фото, эмбеддинг)].

Файлы лежат в каталоге ``known_faces/``: ``Имя.jpg``, ``Имя_2.jpg``, …
Эмбеддинги кэшируются в памяти и обновляются без перезапуска сервера
(добавление/удаление/переименование через админ-панель).

Все обращения к dlib идут через :mod:`facegate.recognition.engine` и его
глобальную блокировку инференса — хранилище **не может** вызвать гонку с
потоком распознавания (см. документацию модуля engine).

Запись фото выполняется через :mod:`facegate.imaging` (``cv2.imencode`` +
запись байтов), поэтому пути с кириллицей и права доступа к каталогу не
приводят к «тихому» отсутствию файла: любая ошибка возвращается как
``(False, сообщение)`` и попадает в ответ админ-панели.
"""
import logging
import os
import re
import threading

import cv2
import numpy as np

from facegate import imaging
from facegate.recognition import engine as recog

log = logging.getLogger(__name__)

IMAGE_EXTS = (".jpg", ".jpeg", ".png")
_SUFFIX_RE = re.compile(r"_\d+$")

JPEG_QUALITY = 90


def sanitize_name(name):
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", str(name))
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:64].rstrip(" .")


class FaceStore:
    """Потокобезопасное хранилище эталонов: имя -> [(файл, эмбеддинг)]."""

    def __init__(self, faces_dir):
        self.faces_dir = faces_dir
        ok, err = imaging.ensure_directory(faces_dir)
        if not ok:
            # каталог может быть создан позже (том Docker), поэтому не падаем,
            # но loudly сообщаем — иначе фото «не сохранялись» без причины
            log.error("Каталог эталонов недоступен: %s", err)
        self._lock = threading.RLock()
        self._persons = {}

    # ---------- загрузка ----------
    def load(self, timeout=None):
        persons = {}
        try:
            names = sorted(os.listdir(self.faces_dir))
        except OSError as e:
            log.error("Не удалось прочитать каталог эталонов %s: %s", self.faces_dir, e)
            with self._lock:
                self._persons = persons
            return {}
        for fname in names:
            if not fname.lower().endswith(IMAGE_EXTS):
                continue
            name = _SUFFIX_RE.sub("", os.path.splitext(fname)[0])
            if not name:
                continue
            path = os.path.join(self.faces_dir, fname)
            try:
                img, encs = recog.load_image_file(path, timeout=timeout)
            except recog.InferenceBusyError as e:
                log.error("Пропущен эталон %s: %s", fname, e)
                continue
            except Exception as e:
                log.error(f"Ошибка загрузки {fname}: {e}")
                continue
            if not encs:
                # Эталон мог быть сохранён плотным кропом (фото из кадра панели):
                # обычный детектор на нём лица не видит, пробуем эвристику с полями.
                try:
                    found = recog.detect_in_crop(img, assume_rgb=True, timeout=timeout)
                except recog.InferenceBusyError as e:
                    log.error("Пропущен эталон %s: %s", fname, e)
                    continue
                if found:
                    encs = [found[0][1]]
                    log.info("Эталон %s: лицо найдено на кропе (эвристика полей)", fname)
            if not encs:
                log.warning(f"В файле {fname} не найдено лиц")
                continue
            persons.setdefault(name, []).append((fname, np.asarray(encs[0])))
            log.info(f"Загружен эталон: {name} ({fname})")
        with self._lock:
            self._persons = persons
        total = sum(len(v) for v in persons.values())
        log.info(f"Всего эталонов: {total} ({len(persons)} чел.)")
        return dict(persons)

    # ---------- чтение ----------
    def names(self):
        with self._lock:
            return sorted(self._persons.keys())

    def has(self, name):
        with self._lock:
            return name in self._persons

    def photos(self, name):
        with self._lock:
            return [f for f, _ in self._persons.get(name, [])]

    def entries(self):
        """Снимок [(имя, эмбеддинг)] для потока распознавания."""
        with self._lock:
            return [(n, enc.copy()) for n, lst in self._persons.items() for _, enc in lst]

    def total_photos(self):
        with self._lock:
            return sum(len(v) for v in self._persons.values())

    def info(self):
        """Сводка для админ-панели."""
        with self._lock:
            return {
                "dir": self.faces_dir,
                "persons": len(self._persons),
                "photos": sum(len(v) for v in self._persons.values()),
            }

    # ---------- добавление ----------
    def _next_filename(self, safe):
        existing_lower = {f.lower() for f in os.listdir(self.faces_dir)}
        candidate = f"{safe}.jpg"
        idx = 1
        while candidate.lower() in existing_lower and idx < 99:
            idx += 1
            candidate = f"{safe}_{idx}.jpg"
        return candidate

    def add_photo(self, name, data, embedding=None, timeout=None):
        """Проверяет фото, сохраняет его и обновляет кэш. Возвращает (ok, message).

        ``data`` — байты изображения (JPEG/PNG).

        ``embedding`` — готовый 128-мерный дескриптор лица. Если он передан
        (обычный случай для «добавить лицо из кадра»), повторная детекция
        **не выполняется**: HOG-детектор на плотно обрезанном лице почти всегда
        возвращает 0 лиц, из-за чего фото из панели не сохранялось (422).

        Если эмбеддинг не передан, фото проверяется детектором; для плотных
        кропов включается эвристика с полями и увеличением
        (:func:`facegate.recognition.engine.detect_in_crop`).
        """
        safe = sanitize_name(name)
        if not safe:
            return False, "Пустое или некорректное имя"
        img = imaging.decode_image(data)
        if img is None:
            return False, "Не удалось декодировать изображение"
        try:
            recog.face_engine()
        except RuntimeError as e:
            return False, str(e)

        enc = None
        if embedding is not None:
            enc = np.asarray(embedding, dtype=np.float32).reshape(-1)
            if enc.size == 0:
                enc = None
        if enc is None:
            enc, err = self._embedding_for_photo(img, timeout=timeout)
            if enc is None:
                return False, err

        # сохраняем исходное изображение в JPEG (единый формат базы эталонов)
        jpeg = imaging.encode_image(img, ".jpg", JPEG_QUALITY)
        if jpeg is None:
            return False, "Не удалось перекодировать изображение"
        with self._lock:
            try:
                fname = self._next_filename(safe)
            except OSError as e:
                log.error("Не удалось прочитать каталог эталонов: %s", e)
                return False, f"Каталог эталонов недоступен: {e}"
            path = os.path.join(self.faces_dir, fname)
            ok, message = imaging.write_bytes(path, jpeg)
            if not ok:
                log.error("Не удалось сохранить эталон %s: %s", path, message)
                return False, message
            self._persons.setdefault(safe, []).append((fname, enc))
        log.info(f"Добавлен эталон: {safe} ({fname})")
        return True, fname

    def _embedding_for_photo(self, img, timeout=None):
        """Эмбеддинг загруженного фото: сначала «как есть», затем кроп-эвристики.

        Возвращает (вектор|None, сообщение об ошибке).
        """
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        try:
            locations, encodings = recog.detect_with_embeddings(rgb, timeout=timeout)
        except recog.InferenceBusyError as e:
            return None, str(e)
        except Exception as e:  # pragma: no cover - ошибки dlib на битых файлах
            log.error("Ошибка детекции лиц: %s", e)
            locations, encodings = [], []
        if locations and encodings:
            return np.asarray(encodings[0]), ""
        # Плотно обрезанное фото (кроп из панели, скан документа, аватар):
        # детектор ищет лицо с контекстом, поэтому пробуем вариант с полями.
        try:
            found = recog.detect_in_crop(img, timeout=timeout)
        except recog.InferenceBusyError as e:
            return None, str(e)
        if found:
            return np.asarray(found[0][1]), ""
        return None, ("На фото не найдено лицо (попробуйте кадр, где лицо крупно "
                      "и без сильного наклона)")

    # ---------- удаление / переименование ----------
    def delete_photo(self, name, fname):
        """Удаляет одно фото человека. Возвращает (ok, message).

        Если фото было последним — человек исчезает из базы.
        """
        fname = os.path.basename(fname or "")
        with self._lock:
            lst = self._persons.get(name)
            if not lst:
                return False, "Человек не найден"
            entry = next((item for item in lst if item[0] == fname), None)
            if entry is None:
                return False, "Фото не найдено"
            try:
                os.remove(os.path.join(self.faces_dir, fname))
            except OSError as e:
                return False, f"Не удалось удалить файл: {e}"
            lst.remove(entry)
            if not lst:
                self._persons.pop(name, None)
            # переименовываем оставшиеся файлы, чтобы суффиксы шли подряд
            self._reindex(name)
        log.info("Удалено фото %s у %s", fname, name)
        return True, "Фото удалено"

    def rename_person(self, old_name, new_name):
        """Переименовывает человека и его файлы. Возвращает (ok, message)."""
        safe_new = sanitize_name(new_name)
        if not safe_new:
            return False, "Некорректное новое имя"
        with self._lock:
            lst = self._persons.pop(old_name, None)
            if not lst:
                return False, "Человек не найден"
            if safe_new == old_name:
                self._persons[old_name] = lst
                return False, "Имя не изменилось"
            existing = self._persons.get(safe_new)
            if existing:
                # объединяем: новые файлы получают следующие суффиксы
                lst = existing + lst
            renamed = []
            for _old_fname, enc in lst:
                fname = self._next_filename(safe_new)
                try:
                    os.replace(os.path.join(self.faces_dir, _old_fname),
                               os.path.join(self.faces_dir, fname))
                except OSError as e:
                    log.error("Не удалось переименовать %s: %s", _old_fname, e)
                    return False, f"Ошибка переименования файла: {e}"
                renamed.append((fname, enc))
            self._persons[safe_new] = renamed
        log.info("Переименован: %s -> %s (%d фото)", old_name, safe_new, len(renamed))
        return True, safe_new

    def _reindex(self, name):
        """Приводит имена файлов к виду Имя.jpg, Имя_2.jpg, … (под локальной блокировкой)."""
        lst = self._persons.get(name)
        if not lst:
            return
        wanted = []
        for idx, (fname, enc) in enumerate(lst):
            target = f"{name}.jpg" if idx == 0 else f"{name}_{idx + 1}.jpg"
            if fname != target and not os.path.exists(os.path.join(self.faces_dir, target)):
                try:
                    os.replace(os.path.join(self.faces_dir, fname),
                               os.path.join(self.faces_dir, target))
                    fname = target
                except OSError as e:
                    log.warning("Не удалось переиндексировать %s: %s", fname, e)
            wanted.append((fname, enc))
        self._persons[name] = wanted

    def delete_person(self, name):
        with self._lock:
            lst = self._persons.pop(name, None)
        if not lst:
            return False
        for fname, _ in lst:
            try:
                os.remove(os.path.join(self.faces_dir, fname))
                log.info(f"Удалён файл эталона: {fname}")
            except OSError as e:
                log.error(f"Не удалось удалить {fname}: {e}")
        return True
