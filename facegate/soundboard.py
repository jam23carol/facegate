# -*- coding: utf-8 -*-
"""Библиотека звуков для вкладки «Саундборд» админ-панели.

Источники звуков:
  * **загруженные файлы** (WAV/MP3/OGG/FLAC/M4A) из каталога ``soundboard/``
    (в Docker подключается отдельным томом);
  * **синтезированная озвучка** — приветствия и объявления из кэша
    :class:`facegate.tts.cache.VoiceCache` (по индексу ``voice_cache/index.json``).

Каждый элемент можно:
  * проиграть в браузере (``GET /api/soundboard/<id>/file``);
  * транслировать клиентам через колонки (``POST /api/soundboard/<id>/broadcast``
    — не-WAV конвертируется в WAV через ffmpeg, если он установлен);
  * скрыть/показать (флаг хранится в state-файле, сами файлы не трогаются);
  * удалить.

Идентификаторы элементов: ``upload:<имя файла>`` и ``synth:<digest>``.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

log = logging.getLogger(__name__)

AUDIO_EXTS = (".wav", ".mp3", ".ogg", ".flac", ".m4a")
MIME_BY_EXT = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# Частота WAV, который уходит клиентам (см. комментарий в wav_bytes_for_broadcast)
BROADCAST_SAMPLE_RATE = int(os.environ.get("FACEGATE_AUDIO_RATE", "48000") or 48000)
_UNSAFE_RE = re.compile(r"[^\w\s.\-()\[\]]+", re.UNICODE)
_ID_RE = re.compile(r"^(upload|synth):(.+)$", re.S)


def sanitize_filename(name):
    """Безопасное имя файла: без путей, спецсимволов, ограниченной длины.

    При усечении длины расширение сохраняется (``xxx…xxx.wav``).
    """
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = _UNSAFE_RE.sub("_", base)
    base = re.sub(r"\s+", " ", base).strip(" .")
    if len(base) > 80:
        stem, ext = os.path.splitext(base)
        base = stem[:max(1, 80 - len(ext))] + ext
    return base


class Soundboard:
    """Каталог звуков + состояние «скрытых» элементов."""

    def __init__(self, sounds_dir="soundboard", state_path=None, voice=None):
        # Абсолютный путь: элементы саундборда (item["path"]) попадают во
        # Flask send_file(), который трактует относительные пути от каталога
        # модуля приложения, а не от cwd процесса.
        self.sounds_dir = os.path.abspath(sounds_dir)
        # state_path=None → состояние только в памяти (удобно для тестов)
        self.state_path = os.path.abspath(state_path) if state_path else None
        self.voice = voice
        self._lock = threading.RLock()
        self._hidden = {}
        self._load_state()

    # ---------- состояние (скрытые элементы) ----------
    def _load_state(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            hidden = data.get("hidden") if isinstance(data, dict) else None
            if isinstance(hidden, dict):
                self._hidden = {str(k): float(v) for k, v in hidden.items()}
        except (OSError, ValueError):
            self._hidden = {}

    def _save_state(self):
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "hidden": self._hidden}, f,
                          ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except OSError as e:  # pragma: no cover
            log.warning("Не удалось сохранить состояние саундборда: %s", e)

    def is_hidden(self, item_id):
        with self._lock:
            return item_id in self._hidden

    def set_hidden(self, item_id, hidden=True):
        with self._lock:
            if hidden:
                self._hidden[item_id] = time.time()
            else:
                self._hidden.pop(item_id, None)
            self._save_state()
        return True

    # ---------- элементы ----------
    @staticmethod
    def parse_id(item_id):
        """(kind, name) из идентификатора или (None, None)."""
        m = _ID_RE.match(str(item_id or ""))
        if not m:
            return None, None
        return m.group(1), m.group(2)

    def _upload_items(self):
        try:
            names = sorted(os.listdir(self.sounds_dir))
        except OSError:
            return []
        items = []
        for fname in names:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            path = os.path.join(self.sounds_dir, fname)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            item_id = f"upload:{fname}"
            items.append({
                "id": item_id,
                "kind": "upload",
                "title": os.path.splitext(fname)[0],
                "filename": fname,
                "ext": ext,
                "mimetype": MIME_BY_EXT.get(ext, "application/octet-stream"),
                "size": st.st_size,
                "created": st.st_mtime,
                "hidden": self.is_hidden(item_id),
                "path": path,
                "text": "",
                "name": None,
            })
        return items

    def _synth_items(self):
        voice = self.voice
        if voice is None or not hasattr(voice, "list_synthesized"):
            return []
        try:
            entries = voice.list_synthesized()
        except Exception:  # pragma: no cover - саундборд не должен падать
            log.exception("Не удалось получить список синтезированной озвучки")
            return []
        items = []
        for e in entries:
            items.append({
                "id": e["id"],
                "kind": "synth",
                "title": e.get("title") or e.get("digest"),
                "filename": os.path.basename(e["path"]),
                "ext": ".wav",
                "mimetype": "audio/wav",
                "size": e.get("size") or 0,
                "created": e.get("created") or 0,
                "hidden": self.is_hidden(e["id"]),
                "path": e["path"],
                "text": e.get("text") or "",
                "name": e.get("name"),
                "voice_kind": e.get("kind"),
            })
        return items

    def list_items(self, include_hidden=False):
        with self._lock:
            items = self._upload_items() + self._synth_items()
        if not include_hidden:
            items = [i for i in items if not i["hidden"]]
        return items

    def find(self, item_id):
        for item in self.list_items(include_hidden=True):
            if item["id"] == item_id:
                return item
        return None

    # ---------- операции ----------
    def add_upload(self, filename, data, title=None):
        """Сохраняет загруженный файл. Возвращает (ok, item или текст ошибки)."""
        safe = sanitize_filename(filename)
        ext = os.path.splitext(safe)[1].lower()
        if not safe or ext not in AUDIO_EXTS:
            return False, ("Недопустимый файл: поддерживаются "
                           + ", ".join(AUDIO_EXTS))
        if data is None or len(data) == 0:
            return False, "Пустой файл"
        if len(data) > MAX_UPLOAD_BYTES:
            return False, "Файл больше 25 МБ"
        try:
            os.makedirs(self.sounds_dir, exist_ok=True)
        except OSError as e:
            return False, f"Не удалось создать каталог {self.sounds_dir}: {e}"
        target = safe
        idx = 1
        with self._lock:
            while os.path.exists(os.path.join(self.sounds_dir, target)):
                idx += 1
                target = f"{os.path.splitext(safe)[0]}_{idx}{ext}"
                if idx > 99:
                    return False, "Слишком много файлов с таким именем"
            path = os.path.join(self.sounds_dir, target)
            tmp = path + ".part"
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except OSError as e:
                return False, f"Ошибка записи: {e}"
        item_id = f"upload:{target}"
        log.info("Саундборд: добавлен файл %s (%d байт)", target, len(data))
        return True, {
            "id": item_id, "kind": "upload",
            "title": (str(title).strip()[:80] if title else
                      os.path.splitext(target)[0]),
            "filename": target, "ext": ext,
            "mimetype": MIME_BY_EXT.get(ext, "application/octet-stream"),
            "size": len(data), "created": time.time(),
            "hidden": False, "path": path, "text": "", "name": None,
        }

    def delete(self, item_id):
        """Удаляет элемент. Возвращает (ok, message)."""
        kind, name = self.parse_id(item_id)
        if kind == "upload":
            if os.path.basename(name) != name:
                return False, "Некорректное имя файла"
            path = os.path.join(self.sounds_dir, name)
            try:
                os.remove(path)
            except OSError:
                return False, "Файл не найден"
        elif kind == "synth":
            voice = self.voice
            if voice is None or not hasattr(voice, "delete_synthesized"):
                return False, "Кэш озвучки недоступен"
            # ключ кэша: 16 шестнадцатеричных символов и, для вариантов
            # приветствия из пула фраз, суффикс «_N» (см. VoiceCache)
            if not re.fullmatch(r"[0-9a-f]{16}(?:_\d{1,3})?", name or ""):
                return False, "Некорректный идентификатор"
            if not voice.delete_synthesized(name):
                return False, "Файл озвучки не найден"
        else:
            return False, "Неизвестный элемент"
        with self._lock:
            self._hidden.pop(item_id, None)
            self._save_state()
        log.info("Саундборд: удалён %s", item_id)
        return True, "Удалено"

    def resolve(self, item_id):
        """Элемент по id (с реальным путём) или None."""
        kind, name = self.parse_id(item_id)
        if kind == "upload":
            if not name or os.path.basename(name) != name:
                return None
            path = os.path.join(self.sounds_dir, name)
            if not os.path.isfile(path):
                return None
        item = self.find(item_id)
        if item is None or not os.path.isfile(item.get("path", "")):
            return None
        return item

    # ---------- трансляция клиентам ----------
    @staticmethod
    def ffmpeg_available():
        return bool(shutil.which("ffmpeg"))

    def wav_bytes_for_broadcast(self, item_id, timeout=30.0):
        """WAV-байты элемента для отправки клиенту. Возвращает (bytes|None, error|None).

        Не-WAV форматы конвертируются ffmpeg (если установлен): клиент играет
        WAV системным плеером (paplay/aplay), который mp3/ogg не понимает.
        """
        item = self.resolve(item_id)
        if item is None:
            return None, "Элемент не найден"
        path = item["path"]
        try:
            if item.get("ext") == ".wav":
                with open(path, "rb") as f:
                    return f.read(), None
        except OSError as e:
            return None, f"Не удалось прочитать файл: {e}"
        if not self.ffmpeg_available():
            return None, ("Для трансляции не-WAV нужен ffmpeg на сервере "
                          "(в Docker-образе он есть)")
        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="sb_")
        os.close(fd)
        try:
            # 48 кГц — аппаратная частота большинства звуковых карт: клиент
            # играет такой WAV без передискретизации (24 кГц ALSA/DirectSound
            # либо отклоняли, либо играли вдвое быстрее)
            proc = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
                 "-ar", str(BROADCAST_SAMPLE_RATE), "-ac", "1", tmp],
                capture_output=True, text=True, timeout=timeout)
            if proc.returncode != 0 or os.path.getsize(tmp) <= 44:
                return None, f"ffmpeg не смог конвертировать: {(proc.stderr or '')[-200:]}"
            with open(tmp, "rb") as f:
                return f.read(), None
        except subprocess.TimeoutExpired:
            return None, "Таймаут конвертации ffmpeg"
        except OSError as e:
            return None, f"Ошибка конвертации: {e}"
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
