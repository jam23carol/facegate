# -*- coding: utf-8 -*-
"""Низкоуровневая работа с face_recognition/dlib.

Здесь живут все функции, которым нужен сам ``face_recognition``:

* :func:`require_face_recognition` — понятная ошибка, если dlib не установлен;
* :func:`face_engine`              — текущий модуль (учитывает подмену в тестах);
* :func:`downscale_for_analysis`   — уменьшение кадра перед анализом;
* :func:`detect_faces_for_capture` — поиск лиц + эмбеддинги (для панели:
  «добавить лицо из текущего кадра»);
* :func:`recognize_face`           — сравнение эмбеддинга с эталонами;
* :func:`crop_face` / :func:`encode_jpeg` — кроп лица и кодирование снимка.

Потокобезопасность (важно!)
---------------------------
``face_recognition`` использует **глобальные синглтоны** C++-моделей dlib
(``dlib.face_recognition_model_v1``, ``dlib.shape_predictor``) и общий
OpenMP/BLAS-рантайм. Эти объекты **не потокобезопасны**: два одновременных
вызова детекции/эмбеддингов из разных потоков (поток распознавания + HTTP-воркер
Flask, который добавляет лицо из кадра) приводят к взаимной блокировке на
уровне futex/OpenMP или к порче памяти — процесс сервера зависал навсегда.

Поэтому **любой** вызов dlib в проекте проходит через одну глобальную
блокировку :data:`INFERENCE_LOCK` (:func:`inference_lock`). Блокировка
реентерабельная (вложенные вызовы в том же потоке не deadlock-ятся) и берётся
**с таймаутом**: если движок занят дольше :data:`INFERENCE_TIMEOUT`, вызывающий
поток получает :class:`InferenceBusyError`, а не вечное зависание. HTTP-API
превращает её в понятный ответ 503 «движок распознавания занят».

Дополнительно ограничиваются нативные пулы потоков (OpenMP/MKL/OpenBLAS —
см. ``facegate/__init__.py`` и ``dlib.set_num_threads``): многопоточный OpenMP
внутри одного вызова dlib — вторая типовая причина зависаний на CPU.

Тесты подменяют модуль ``face_recognition`` заглушкой через
``monkeypatch.setattr(facegate.recognition.engine, "face_recognition", fake)`` —
все функции читают атрибут модуля динамически, поэтому dlib для тестов не нужен.
"""
import contextlib
import logging
import os
import threading
import time

import cv2
import numpy as np

try:  # dlib/face_recognition нужны не всегда (например, в веб-панели без распознавания)
    import face_recognition
except ImportError as _e:  # pragma: no cover
    face_recognition = None
    _FACE_RECOGNITION_IMPORT_ERROR = _e
else:
    _FACE_RECOGNITION_IMPORT_ERROR = None

try:  # dlib нужен только чтобы ограничить число его нативных потоков
    import dlib
except ImportError:  # pragma: no cover - dlib не установлен
    dlib = None

from facegate.imaging import encode_jpeg as _encode_jpeg  # noqa: F401 (ре-экспорт)

log = logging.getLogger(__name__)

# Ширина кадра, на которой ищем лица при добавлении человека из кадра:
# достаточно крупно для качества эталона и достаточно быстро на CPU.
CAPTURE_DETECT_WIDTH = 1024

# Минимальный размер лица (px) для HOG: детектор не видит совсем мелкие лица,
# поэтому кроп перед детекцией при необходимости увеличивается.
MIN_DETECT_FACE_PX = 110

# ---------------------------------------------------------------------------
# Глобальная блокировка инференса dlib
# ---------------------------------------------------------------------------
#: Одна блокировка на весь процесс — все вызовы dlib строго последовательны.
INFERENCE_LOCK = threading.RLock()

#: Сколько секунд HTTP-воркер готов ждать освобождения движка (env:
#: ``FACEGATE_INFERENCE_TIMEOUT``). После этого — :class:`InferenceBusyError`
#: и понятный ответ 503, вместо «сервер завис навсегда».
INFERENCE_TIMEOUT = float(os.environ.get("FACEGATE_INFERENCE_TIMEOUT", "30") or 30)

_threads_limited = False
_threads_lock = threading.Lock()

# Счётчик потоков, ожидающих блокировку: нужен, чтобы «горячий» поток
# распознавания уступал движок HTTP-воркерам панели (иначе при высоком FPS
# они голодали и получали 503, хотя дедлока уже нет).
_WAITERS = 0
_WAITERS_LOCK = threading.Lock()


def _enter_wait():
    global _WAITERS
    with _WAITERS_LOCK:
        _WAITERS += 1


def _exit_wait():
    global _WAITERS
    with _WAITERS_LOCK:
        _WAITERS = max(0, _WAITERS - 1)


def waiters():
    """Сколько потоков сейчас ждут доступ к движку распознавания."""
    with _WAITERS_LOCK:
        return _WAITERS


class InferenceBusyError(RuntimeError):
    """Движок распознавания занят другим потоком дольше отведённого таймаута."""


def limit_native_threads(num_threads=None):
    """Ограничивает нативные пулы потоков dlib/OpenMP (вызывается один раз).

    Один поток OpenMP внутри dlib устраняет гонки/дедлоки параллельного
    рантайма; распознавание при этом остаётся однопоточным и предсказуемым,
    а свободные ядра используются веб-панелью, TTS и ZeroMQ.
    """
    global _threads_limited
    with _threads_lock:
        if _threads_limited:
            return
        _threads_limited = True
    try:
        n = int(num_threads if num_threads is not None
                else os.environ.get("FACEGATE_DLIB_THREADS", "1"))
    except (TypeError, ValueError):
        n = 1
    setter = getattr(dlib, "set_num_threads", None) if dlib is not None else None
    if setter is None:
        return
    try:
        setter(max(1, n))
        log.debug("dlib.set_num_threads(%d)", max(1, n))
    except Exception as e:  # pragma: no cover - зависит от сборки dlib
        log.debug("Не удалось ограничить потоки dlib: %s", e)


@contextlib.contextmanager
def inference_lock(timeout=None, who="inference", polite=False, polite_wait=0.25):
    """Контекст-менеджер глобальной блокировки инференса dlib.

    ``timeout=None`` → :data:`INFERENCE_TIMEOUT`; ``timeout=0`` → ждать вечно
    (используется фоновым потоком распознавания).

    ``polite=True`` — перед захватом пропустить вперёд потоки, которые уже ждут
    блокировку (поток распознавания при высоком FPS иначе «голодал» бы
    HTTP-воркеры панели). Вежливость ограничена ``polite_wait`` секундами,
    поэтому само распознавание не может быть заблокировано навсегда.
    """
    limit_native_threads()
    wait = INFERENCE_TIMEOUT if timeout is None else float(timeout)
    if polite:
        deadline = time.time() + max(0.0, float(polite_wait))
        while waiters() > 0 and time.time() < deadline:
            time.sleep(0.002)
    if not INFERENCE_LOCK.acquire(timeout=0):
        _enter_wait()
        try:
            acquired = (INFERENCE_LOCK.acquire(timeout=wait) if wait > 0
                        else INFERENCE_LOCK.acquire())
        finally:
            _exit_wait()
    else:
        acquired = True
    if not acquired:
        raise InferenceBusyError(
            f"Движок распознавания занят ({who}): не удалось получить доступ "
            f"за {wait:.0f} с. Повторите операцию позже.")
    try:
        yield
    finally:
        INFERENCE_LOCK.release()


def inference_locked(timeout=0.0):
    """True, если блокировка инференса занята ДРУГИМ потоком (для диагностики).

    Блокировка реентерабельна, поэтому из потока-владельца функция всегда
    возвращает False — проверяйте состояние из другого потока.
    """
    got = INFERENCE_LOCK.acquire(timeout=max(0.0, float(timeout or 0.0)))
    if got:
        INFERENCE_LOCK.release()
        return False
    return True


# ---------------------------------------------------------------------------
# Доступ к движку
# ---------------------------------------------------------------------------
def require_face_recognition():
    """Понятная ошибка, если dlib/face_recognition не установлены."""
    if face_recognition is None:
        raise RuntimeError(
            "Не установлен face_recognition/dlib: "
            f"{_FACE_RECOGNITION_IMPORT_ERROR}. "
            "Установите зависимости: python -m pip install --force-reinstall \"setuptools<70\" "
            "и pip install -r requirements-prebuilt.txt"
        )
    return face_recognition


def face_engine():
    """Текущий модуль face_recognition (уважает подмену заглушкой в тестах)."""
    return face_recognition if face_recognition is not None else require_face_recognition()


def downscale_for_analysis(frame, max_width):
    """Уменьшает кадр для анализа. Возвращает (кадр, коэффициент масштабирования).

    Коэффициент нужен, чтобы вернуть координаты рамок в масштаб исходного кадра
    (:func:`scale_box`).
    """
    if not max_width or max_width <= 0:
        return frame, 1.0
    h, w = frame.shape[:2]
    if w <= max_width:
        return frame, 1.0
    factor = w / float(max_width)
    small = cv2.resize(frame, (max_width, max(1, int(round(h / factor)))),
                       interpolation=cv2.INTER_AREA)
    return small, factor


def scale_box(box, factor):
    """Масштабирует (top, right, bottom, left) обратно в координаты исходного кадра."""
    top, right, bottom, left = box
    return (int(round(top * factor)), int(round(right * factor)),
            int(round(bottom * factor)), int(round(left * factor)))


def box_to_dict(box):
    """(top, right, bottom, left) → {"left","top","right","bottom"} для HTTP API."""
    top, right, bottom, left = box
    return {"left": int(left), "top": int(top), "right": int(right), "bottom": int(bottom)}


# ---------------------------------------------------------------------------
# Примитивы dlib (всегда под глобальной блокировкой)
# ---------------------------------------------------------------------------
def face_locations(rgb, model="hog", timeout=None):
    """``face_recognition.face_locations`` под глобальной блокировкой инференса."""
    fr = face_engine()
    with inference_lock(timeout=timeout, who="face_locations"):
        return fr.face_locations(rgb, model=model)


def face_encodings(rgb, locations=None, timeout=None):
    """``face_recognition.face_encodings`` под глобальной блокировкой инференса."""
    fr = face_engine()
    with inference_lock(timeout=timeout, who="face_encodings"):
        if locations:
            return fr.face_encodings(rgb, locations)
        return fr.face_encodings(rgb)


def load_image_file(path, timeout=None):
    """Загрузка эталона с диска + эмбеддинги (под блокировкой)."""
    fr = face_engine()
    with inference_lock(timeout=timeout, who="load_image_file"):
        img = fr.load_image_file(path)
        return img, fr.face_encodings(img)


def detect_with_embeddings(rgb, model="hog", timeout=None, polite=False):
    """(boxes, encodings) одним вызовом под одной блокировкой.

    Отдельные вызовы ``face_locations`` и ``face_encodings`` из разных потоков
    — ровно та ситуация, которая приводила к дедлоку; здесь оба вызова
    гарантированно выполняются последовательно в одном критическом разделе.

    ``polite=True`` — используется потоком распознавания: он уступает движок
    запросам админ-панели, которые уже стоят в очереди.
    """
    fr = face_engine()
    with inference_lock(timeout=timeout, who="detect_with_embeddings", polite=polite):
        locations = fr.face_locations(rgb, model=model)
        if not locations:
            return [], []
        return locations, fr.face_encodings(rgb, locations)


def detect_faces_for_capture(frame, model="hog", max_width=CAPTURE_DETECT_WIDTH,
                             timeout=None):
    """Ищет лица в кадре и возвращает [(box_dict, embedding)] в координатах кадра.

    Используется веб-панелью для добавления нового лица из текущего изображения.
    Бросает RuntimeError, если face_recognition не установлен, и
    :class:`InferenceBusyError`, если движок занят другим потоком.
    """
    face_engine()  # понятная ошибка, если dlib не установлен
    proc, factor = downscale_for_analysis(frame, max_width)
    rgb = cv2.cvtColor(proc, cv2.COLOR_BGR2RGB)
    locations, encodings = detect_with_embeddings(rgb, model=model, timeout=timeout)
    out = []
    for box, enc in zip(locations, encodings):
        top, right, bottom, left = scale_box(box, factor)
        out.append((box_to_dict((top, right, bottom, left)), np.asarray(enc)))
    return out


def recognize_face(entries, face_encoding, threshold):
    """Сравнивает эмбеддинг с эталонами.

    ``entries`` — снимок из FaceStore.entries(): [(имя, эмбеддинг)].
    Возвращает (имя или None, дистанция).

    ``face_recognition.face_distance`` — чистый numpy (нормы векторов), dlib не
    вызывается, поэтому блокировка инференса здесь не нужна.
    """
    if not entries:
        return None, 1.0
    fr = face_engine()
    names = [n for n, _ in entries]
    matrix = np.stack([enc for _, enc in entries])
    distances = fr.face_distance(matrix, face_encoding)
    best_idx = int(np.argmin(distances))
    if distances[best_idx] < threshold:
        return names[best_idx], float(distances[best_idx])
    return None, float(distances[best_idx])


# ---------------------------------------------------------------------------
# Кроп лица: подготовка изображения и эмбеддинг «из коробки»
# ---------------------------------------------------------------------------
def crop_face(frame, left, top, right, bottom, margin=0.25):
    """Кроп области лица с отступами (в долях размера рамки), границы кадра учитываются.

    Возвращает numpy-кадр (BGR) или None, если область вырожденная.
    """
    h, w = frame.shape[:2]
    bw = max(1, int(right) - int(left))
    bh = max(1, int(bottom) - int(top))
    mx = int(bw * margin)
    my = int(bh * margin)
    x1 = max(0, int(left) - mx)
    y1 = max(0, int(top) - my)
    x2 = min(w, int(right) + mx)
    y2 = min(h, int(bottom) + my)
    if x2 <= x1 or y2 <= y1:
        return None
    return frame[y1:y2, x1:x2]


def pad_for_detection(image, pad=0.6, min_side=MIN_DETECT_FACE_PX, max_side=1600):
    """Дополняет плотный кроп полями и при необходимости увеличивает.

    HOG-детектор dlib обучен искать лицо **вместе с контекстом** (контуры
    головы/плеч). На плотно обрезанном фрагменте лица он почти всегда
    возвращает 0 лиц — именно поэтому ``store.add_photo`` отказывался
    сохранять лицо, вырезанное из кадра панели. Поля ``BORDER_REPLICATE``
    возвращают детектору ожидаемый контекст, а увеличение — минимальный
    размер лица, на котором HOG вообще работает.
    """
    if image is None or getattr(image, "size", 0) == 0:
        return image, 1.0
    h, w = image.shape[:2]
    top = int(h * pad)
    bottom = int(h * pad * 0.6)
    left = int(w * pad)
    right = int(w * pad)
    padded = cv2.copyMakeBorder(image, top, bottom, left, right,
                                cv2.BORDER_REPLICATE)
    ph, pw = padded.shape[:2]
    short = min(ph, pw)
    scale = 1.0
    if short and short < min_side:
        scale = min_side / float(short)
    if max(ph, pw) * scale > max_side:
        scale = min(scale, max_side / float(max(ph, pw)))
    if scale > 1.02:
        padded = cv2.resize(padded, (max(1, int(round(pw * scale))),
                                     max(1, int(round(ph * scale)))),
                            interpolation=cv2.INTER_CUBIC)
    return padded, scale


def _dlib_rect(box):
    """dlib.rectangle по словарю/кортежу коробки или None, если dlib недоступен."""
    if dlib is None:
        return None
    try:
        if isinstance(box, dict):
            left, top = int(box["left"]), int(box["top"])
            right, bottom = int(box["right"]), int(box["bottom"])
        else:  # (top, right, bottom, left) — формат face_recognition
            top, right, bottom, left = (int(v) for v in box)
        return dlib.rectangle(left, top, right, bottom)
    except Exception:  # pragma: no cover - защита от нестандартных коробок
        return None


def embedding_from_box(frame, box, model="hog", timeout=None):
    """Эмбеддинг лица по его рамке в исходном кадре (без повторной детекции).

    Именно так нужно получать дескриптор для «добавить лицо из кадра»: рамка уже
    найдена детектором на полном кадре, а повторный поиск лица на **обрезанном**
    фрагменте почти всегда заканчивается «лицо не найдено».

    Порядок:
      1) dlib.rectangle по рамке на полном кадре (точный и быстрый путь);
      2) запасной путь: кроп с полями → детекция (:func:`detect_in_crop`).

    Возвращает numpy-вектор (128,) или None.
    """
    face_engine()
    rect = _dlib_rect(box)
    if rect is not None:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        try:
            encs = face_encodings(rgb, [rect], timeout=timeout)
        except Exception as e:  # pragma: no cover - зависит от сборки dlib
            log.warning("Не удалось получить эмбеддинг по рамке: %s", e)
            encs = []
        if encs:
            return np.asarray(encs[0])
    if isinstance(box, dict):
        crop = crop_face(frame, box["left"], box["top"], box["right"], box["bottom"],
                         margin=0.35)
    else:
        top, right, bottom, left = box
        crop = crop_face(frame, left, top, right, bottom, margin=0.35)
    if crop is None:
        return None
    found = detect_in_crop(crop, models=(model, "hog", "cnn"), timeout=timeout)
    if not found:
        return None
    return np.asarray(found[0][1])


def detect_in_crop(image, models=("hog", "cnn"), pad=0.6, timeout=None,
                   assume_rgb=False):
    """Поиск лица на плотно обрезанном фрагменте. Возвращает [(box_dict, embedding)].

    Пробует несколько стратегий, пока не найдёт лицо:
      * исходный кроп (модель из настроек, затем hog);
      * кроп с восстановленными полями (:func:`pad_for_detection`);
      * то же самое моделью cnn (если dlib собран с поддержкой).

    ``assume_rgb=True`` — изображение уже в RGB (так отдаёт снимки
    ``face_recognition.load_image_file``), конвертация не выполняется.

    Функция нужна, чтобы добавление фото из админ-панели (обрезок лица) не
    заканчивалось ошибкой «На фото не найдено лицо».
    """
    face_engine()
    if image is None or getattr(image, "size", 0) == 0:
        return []
    tried = []
    candidates = [image]
    padded, _scale = pad_for_detection(image, pad=pad)
    if padded is not None and padded.shape[:2] != image.shape[:2]:
        candidates.append(padded)
    model_list = []
    for m in list(models or ()) + ["hog", "cnn"]:
        if m and m not in model_list:
            model_list.append(m)
    for variant in candidates:
        rgb = variant if assume_rgb else cv2.cvtColor(variant, cv2.COLOR_BGR2RGB)
        for model in model_list:
            key = (variant.shape[:2], model)
            if key in tried:
                continue
            tried.append(key)
            try:
                locations, encodings = detect_with_embeddings(rgb, model=model,
                                                              timeout=timeout)
            except Exception as e:
                # cnn недоступен без GPU/нужной сборки dlib — пробуем дальше
                log.debug("Детекция model=%s не удалась: %s", model, e)
                if isinstance(e, InferenceBusyError):
                    raise
                continue
            if locations:
                out = []
                for box, enc in zip(locations, encodings):
                    out.append((box_to_dict(box), np.asarray(enc)))
                # ближайшее к центру лицо — то, ради которого делали кроп
                out.sort(key=lambda item: _distance_to_center(item[0], variant.shape))
                return out
    return []


def _distance_to_center(box, shape):
    h, w = shape[:2]
    cx = (box["left"] + box["right"]) / 2.0 - w / 2.0
    cy = (box["top"] + box["bottom"]) / 2.0 - h / 2.0
    return cx * cx + cy * cy


def encode_jpeg(image, quality=92):
    """Кодирует numpy-кадр в JPEG. Возвращает bytes или None."""
    return _encode_jpeg(image, quality)
