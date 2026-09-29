# -*- coding: utf-8 -*-
"""Кэш заранее синтезированных фраз (приветствия и объявления).

Фразы синтезируются ЗАРАНЕЕ (при добавлении фото через веб-панель либо при
старте сервера) и складываются на диск как WAV. В момент распознавания лица
синтез не выполняется — отдаётся готовый файл, задержка минимальна.

Синтез выполняет один из движков :mod:`facegate.tts.engines` (Silero TTS —
по умолчанию в Docker, espeak-ng/pyttsx3 — запасные варианты). Движок
выбирается настройкой ``tts_engine`` (``auto`` = первый доступный) и может
переключаться на лету из админ-панели.

Вариативность приветствий
-------------------------
Настройка ``greet_templates`` задаёт **пул фраз** (по одной на строку). Для
каждого человека синтезируется отдельный WAV на каждую фразу, файлы хранятся
под детерминированными именами ``{digest}_{index}.wav``, а в индексе кэша
(``voice_cache/index.json``) перечислены все варианты. Метод
:meth:`VoiceCache.get_random` возвращает случайную готовую фразу, **исключая
ту, что звучала в прошлый раз** (анти-повтор), вместе с её текстом — текст
уходит в команде ``greet``, поэтому в панели и журнале видно, какая именно
фраза прозвучала.

Объявления
----------
:meth:`VoiceCache.cached_text_wav` — синхронная проверка кэша (без синтеза),
:meth:`VoiceCache.request_text` — «single-flight» фоновый синтез фразы
(повторные запросы той же фразы присоединяются к текущему, а не запускают
новый), :meth:`VoiceCache.wait_text` — ожидание результата с таймаутом.
Благодаря этому панель отправляет объявление **одной** командой со звуком и
никогда не синтезирует фразу повторно, если она уже есть на диске.
"""
import hashlib
import json
import logging
import os
import queue
import random
import threading
import time
import uuid

from facegate.tts import engines
from facegate.tts.textnorm import normalize_for_tts

log = logging.getLogger(__name__)

_UNSET = object()

DEFAULT_TEMPLATE = "Здравствуйте, {name}!"


def format_template(template, name):
    try:
        return template.format(name=name)
    except (KeyError, IndexError, ValueError):
        return template.replace("{name}", name)


def split_templates(value):
    """Строка/список шаблонов → список непустых строк без дубликатов."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        lines = [str(v) for v in value]
    else:
        lines = str(value).replace("\\n", "\n").splitlines()
    out, seen = [], set()
    for line in lines:
        line = line.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


class TextJob:
    """Фоновый синтез одной произвольной фразы (объявления)."""

    __slots__ = ("text", "event", "data", "error", "created", "started")

    def __init__(self, text):
        self.text = text
        self.event = threading.Event()
        self.data = None
        self.error = None
        self.created = time.time()
        self.started = False

    @property
    def done(self):
        return self.event.is_set()


class VoiceCache:
    """Кэш озвучки + фоновая очередь синтеза (приветствия и объявления)."""

    def __init__(self, cache_dir="voice_cache", template=None, templates=None,
                 rate=None, voice_hint=None, engine=None, speed=None,
                 synth_timeout=60.0):
        # Абсолютный путь кэша: пути к WAV попадают в саундборд и затем во
        # Flask send_file(), который разрешает относительные пути от каталога
        # веб-модуля (facegate/web/), а не от cwd — файлы «исчезали».
        cache_dir = os.path.abspath(cache_dir)
        pool = split_templates(templates) or split_templates(template) or [DEFAULT_TEMPLATE]
        self.templates = pool
        # совместимость: .template — первая фраза пула
        self.template = pool[0]
        self.base_dir = cache_dir
        self.cache_dir = os.path.join(cache_dir, "greetings")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.rate = rate
        self.voice_hint = voice_hint
        self.engine_pref = (engine or "auto").strip().lower() or "auto"
        self.speed = speed
        self.synth_timeout = synth_timeout
        self._engine = None
        self._engine_lock = threading.Lock()
        self._no_engine_logged = False
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._pending = set()
        self._errors = {}
        self._last_variant = {}       # name -> key последней прозвучавшей фразы
        self._stop = threading.Event()
        self._index_path = os.path.join(cache_dir, "index.json")
        self._index_lock = threading.Lock()
        self._text_jobs = {}          # text -> TextJob (single-flight)
        self._text_jobs_lock = threading.Lock()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                        name="tts-worker")
        self._worker.start()

    # ---------- движок ----------
    def get_engine(self):
        """Текущий экземпляр движка TTS (ленивое создание; может быть None)."""
        with self._engine_lock:
            if self._engine is None:
                self._engine = engines.get_engine(self.engine_pref)
                if self._engine is None:
                    # один раз на процесс: иначе каждый запрос /api/status
                    # и каждая попытка озвучки писали бы одно и то же ERROR
                    if not self._no_engine_logged:
                        self._no_engine_logged = True
                        log.error("Не найден ни один TTS-движок (silero/espeak/pyttsx3) — "
                                  "озвучка недоступна. Установите espeak-ng "
                                  "(sudo apt install espeak-ng) или Silero TTS "
                                  "(./install.sh --tts); в Docker silero уже есть.")
                else:
                    log.info("TTS-движок: %s (доступны: %s)", self._engine.name,
                             ", ".join(n for n, ok in engines.list_available_engines().items() if ok)
                             or "—")
            return self._engine

    def engine_name(self):
        engine = self.get_engine()
        return engine.name if engine is not None else "не найден"

    def engine_available(self):
        """True, если какой-либо TTS-движок в этом окружении работоспособен."""
        try:
            return self.get_engine() is not None
        except Exception:  # pragma: no cover
            return False

    # ---------- тексты и пути ----------
    def text_for(self, name, index=0):
        """Текст фразы приветствия (по умолчанию — первая)."""
        pool = self.templates or [DEFAULT_TEMPLATE]
        return format_template(pool[min(max(int(index), 0), len(pool) - 1)], name)

    def texts_for(self, name):
        """Все тексты приветствия человека (по числу шаблонов пула)."""
        return [format_template(t, name) for t in (self.templates or [DEFAULT_TEMPLATE])]

    @staticmethod
    def digest_for_name(template, name, index=None):
        """Детерминированный ключ кэша приветствия (зависит от фразы и её номера)."""
        base = format_template(template, name)
        digest = hashlib.sha1(f"greet|{name}|{base}".encode("utf-8")).hexdigest()[:16]
        return digest if index is None else f"{digest}_{int(index)}"

    @staticmethod
    def digest_for_text(text):
        return hashlib.sha1(f"text|{text}".encode("utf-8")).hexdigest()[:16]

    def _path_for_key(self, key):
        return os.path.join(self.cache_dir, f"{key}.wav")

    def variants_for(self, name):
        """Список вариантов приветствия человека.

        Каждый вариант: ``{"index", "template", "text", "key", "path", "ready"}``.
        Дополнительно проверяется «старое» имя файла (кэш предыдущих версий —
        ``sha1(text).wav``), чтобы уже синтезированная озвучка не пропала после
        обновления.
        """
        out = []
        for idx, template in enumerate(self.templates or [DEFAULT_TEMPLATE]):
            text = format_template(template, name)
            key = self.digest_for_name(template, name, idx)
            path = self._path_for_key(key)
            ready = self._usable(path)
            if not ready:
                legacy = os.path.join(
                    self.cache_dir,
                    f"{hashlib.sha1(text.encode('utf-8')).hexdigest()[:16]}.wav")
                if self._usable(legacy):
                    path, ready = legacy, True
            out.append({"index": idx, "template": template, "text": text,
                        "key": key, "path": path, "ready": ready})
        return out

    def wav_path(self, name, index=0):
        """Путь WAV конкретного варианта (по умолчанию — первого)."""
        pool = self.templates or [DEFAULT_TEMPLATE]
        idx = min(max(int(index), 0), len(pool) - 1)
        return self._path_for_key(self.digest_for_name(pool[idx], name, idx))

    def text_wav_path(self, text):
        """Путь кэша для произвольной фразы (объявления из панели)."""
        return self._path_for_key(self.digest_for_text(text))

    # ---------- чтение кэша ----------
    @staticmethod
    def _usable(path):
        try:
            return os.path.getsize(path) > 44
        except OSError:
            return False

    @staticmethod
    def _read(path):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        return data if len(data) > 44 else None

    def get(self, name):
        """WAV первого готового варианта приветствия (или None)."""
        for variant in self.variants_for(name):
            if variant["ready"]:
                data = self._read(variant["path"])
                if data:
                    return data
        return None

    def get_random(self, name):
        """Случайная готовая фраза без повторения предыдущей: (text, wav|None).

        Возвращает текст выбранной фразы — он уходит в команде ``greet``,
        чтобы панель и журнал показывали именно то, что прозвучало.
        """
        ready = [v for v in self.variants_for(name) if v["ready"]]
        if not ready:
            return self.text_for(name), None
        with self._lock:
            last_key = self._last_variant.get(name)
        candidates = [v for v in ready if v["key"] != last_key] or ready
        choice = random.choice(candidates)
        data = self._read(choice["path"])
        if data is None:  # файл пропал между проверкой и чтением
            for variant in ready:
                data = self._read(variant["path"])
                if data:
                    choice = variant
                    break
        if data is None:
            return self.text_for(name), None
        with self._lock:
            self._last_variant[name] = choice["key"]
        return choice["text"], data

    def variants_info(self, name):
        """Сводка по вариантам приветствия (для панели)."""
        variants = self.variants_for(name)
        return {
            "total": len(variants),
            "ready": sum(1 for v in variants if v["ready"]),
            "texts": [v["text"] for v in variants],
        }

    def status(self, name):
        variants = self.variants_for(name)
        with self._lock:
            pending = name in self._pending
            failed = name in self._errors
        if pending:
            return "pending"
        if any(v["ready"] for v in variants):
            # достаточно одного готового варианта, чтобы здороваться со звуком
            return "ready"
        if failed:
            return "error"
        return "missing"

    def missing_variants(self, name):
        return [v for v in self.variants_for(name) if not v["ready"]]

    # ---------- очередь синтеза приветствий ----------
    def ensure(self, name, force=False, wait=False, timeout=None):
        """Гарантировать наличие синтезированных фраз для имени (все варианты пула)."""
        if not force and not self.missing_variants(name):
            return "ready"
        deadline = time.time() + (timeout if timeout is not None else max(30.0, self.synth_timeout))
        with self._lock:
            already_pending = name in self._pending
            if not already_pending:
                self._pending.add(name)
                self._errors.pop(name, None)
        if already_pending:
            if wait:
                while self.status(name) == "pending" and time.time() < deadline:
                    time.sleep(0.05)
                return self.status(name)
            return "pending"
        ev = threading.Event() if wait else None
        self._queue.put((name, force, ev))
        if wait:
            ev.wait(timeout=max(0.0, deadline - time.time()))
        return self.status(name)

    def configure(self, template=_UNSET, rate=_UNSET, voice_hint=_UNSET,
                  engine=_UNSET, speed=_UNSET, templates=_UNSET):
        """Меняет параметры синтеза на лету (вызывается из админ-панели).

        Возвращает список изменившихся полей. Смена движка закрывает старый
        (например, процесс Silero) — новый создастся лениво.
        """
        changed = []
        engine_changed = False
        old = None
        with self._lock:
            if templates is not _UNSET and templates is not None:
                # пул фраз — источник истины; одиночный template его не сужает
                pool = split_templates(templates)
                if pool:
                    if pool != self.templates:
                        self.templates = pool
                        self._last_variant.clear()
                        changed.append("templates")
                    if self.template != pool[0]:
                        self.template = pool[0]
                        if "template" not in changed:
                            changed.append("template")
            elif template is not _UNSET and template:
                single = split_templates(template)
                if single and single != self.templates:
                    self.templates = single
                    self.template = single[0]
                    self._last_variant.clear()
                    changed.append("template")
            if rate is not _UNSET and rate != self.rate:
                self.rate = rate
                changed.append("rate")
            if voice_hint is not _UNSET and voice_hint != self.voice_hint:
                self.voice_hint = voice_hint
                changed.append("voice_hint")
            if speed is not _UNSET and speed != self.speed:
                self.speed = speed
                changed.append("speed")
        if engine is not _UNSET:
            pref = (engine or "auto").strip().lower() or "auto"
            with self._engine_lock:
                if pref != self.engine_pref:
                    self.engine_pref = pref
                    engine_changed = True
                    old, self._engine = self._engine, None
                    self._no_engine_logged = False
            if engine_changed:
                changed.append("engine")
                if old is not None:
                    try:
                        old.close()
                    except Exception:  # pragma: no cover
                        log.exception("Не удалось закрыть предыдущий TTS-движок")
        if changed:
            log.info("Параметры TTS обновлены: %s", ", ".join(changed))
        return changed

    def rebuild(self, names, force=True):
        """Ставит в очередь пересинтез всех имён (после смены шаблона/голоса/движка)."""
        names = list(names or [])
        for name in names:
            self.ensure(name, force=force)
        return len(names)

    def errors(self):
        with self._lock:
            return dict(self._errors)

    def pending_names(self):
        with self._lock:
            return sorted(self._pending)

    def cache_info(self):
        files = total = 0
        try:
            for fname in os.listdir(self.cache_dir):
                path = os.path.join(self.cache_dir, fname)
                if os.path.isfile(path) and fname.lower().endswith(".wav"):
                    files += 1
                    total += os.path.getsize(path)
        except OSError:
            pass
        return {"files": files, "bytes": total, "dir": self.cache_dir,
                "engine": self.engine_name(),
                "engine_pref": self.engine_pref,
                "available_engines": engines.list_available_engines(),
                "rate": self.rate, "speed": self.speed,
                "voice_hint": self.voice_hint, "template": self.template,
                "templates": list(self.templates),
                "variants": len(self.templates),
                "pending": len(self.pending_names()), "errors": len(self.errors())}

    # ---------- произвольные фразы (объявления) ----------
    def cached_text_wav(self, text):
        """WAV фразы из кэша **без синтеза** (None, если ещё не синтезирована)."""
        if not text:
            return None
        return self._read(self.text_wav_path(text))

    def request_text(self, text):
        """Запускает (или присоединяется к) фоновый синтез фразы. Возвращает TextJob.

        «Single-flight»: повторный запрос той же фразы не запускает второй
        синтез — именно так устраняется «озвучка готовится заново» при каждом
        нажатии кнопки «Объявить».
        """
        if not text:
            return None
        with self._text_jobs_lock:
            job = self._text_jobs.get(text)
            if job is not None and not job.done:
                return job
            job = TextJob(text)
            self._text_jobs[text] = job
            cached = self.cached_text_wav(text)
            if cached:
                job.data = cached
                job.event.set()
                return job
        threading.Thread(target=self._text_worker, args=(job,), daemon=True,
                         name="announce-tts").start()
        return job

    def wait_text(self, text, timeout=0.0):
        """Ждёт готовности фразы не дольше ``timeout`` сек. Возвращает WAV или None."""
        job = self.request_text(text)
        if job is None:
            return None
        if job.done:
            return job.data
        if timeout and timeout > 0:
            job.event.wait(timeout=float(timeout))
        return job.data if job.done else None

    def _text_worker(self, job):
        path = self.text_wav_path(job.text)
        tmp = os.path.join(self.cache_dir, f"tmp_{uuid.uuid4().hex[:8]}.wav")
        try:
            job.started = True
            # Нормализация — ДО движка: тире/кавычки/служебные символы могут
            # обрезать фразу у токенизатора Silero (см. facegate.tts.textnorm)
            self._synthesize_in_subprocess(normalize_for_tts(job.text), tmp)
            if not self._usable(tmp):
                raise engines.TTSError("TTS вернул пустой файл")
            with open(tmp, "rb") as f:
                job.data = f.read()
            os.replace(tmp, path)
            self._index_add(self.digest_for_text(job.text), job.text, kind="announce")
            log.info("Озвучка объявления готова: «%s»", job.text[:60])
        except Exception as e:  # noqa: BLE001 - ошибка уходит заказчику фразы
            job.error = str(e)
            log.error("Не удалось синтезировать объявление «%s»: %s", job.text[:60], e)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            job.event.set()
            with self._text_jobs_lock:
                # чистим завершившиеся задачи, чтобы словарь не рос вечно
                for key, item in list(self._text_jobs.items()):
                    if item.done and time.time() - item.created > 60.0:
                        self._text_jobs.pop(key, None)

    def synthesize_now(self, text, timeout=None):
        """Синхронно синтезирует фразу и возвращает байты WAV (или None).

        Сначала проверяется кэш: если фраза уже синтезирована, повторного
        синтеза не происходит. Используется для объявлений из админ-панели.
        """
        if not text:
            return None
        cached = self.cached_text_wav(text)
        if cached:
            return cached
        job = self.request_text(text)
        if job is None:
            return None
        job.event.wait(timeout=float(timeout or max(30.0, self.synth_timeout)))
        return job.data

    # ---------- индекс синтезированных звуков (для саундборда) ----------
    def _index_load(self):
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items") if isinstance(data, dict) else None
            return items if isinstance(items, dict) else {}
        except (OSError, ValueError):
            return {}

    def _index_save(self, items):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._index_path)), exist_ok=True)
            tmp = self._index_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "items": items}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._index_path)
        except OSError as e:  # pragma: no cover - индекс не критичен
            log.warning("Не удалось сохранить индекс озвучки: %s", e)

    def _index_add(self, key, text, kind="greeting", name=None, variant=None):
        with self._index_lock:
            items = self._index_load()
            items[key] = {"text": text, "kind": kind,
                          "name": name or None, "ts": time.time(),
                          "variant": variant, "digest": key}
            self._index_save(items)

    def _index_remove(self, key):
        with self._index_lock:
            items = self._index_load()
            if items.pop(key, None) is not None:
                self._index_save(items)

    def list_synthesized(self):
        """Список синтезированных фраз для саундборда (только существующие файлы)."""
        out = []
        with self._index_lock:
            items = self._index_load()
        for key, meta in items.items():
            path = self._path_for_key(key)
            if not self._usable(path):
                # кэш предыдущих версий: ключ = sha1(text), файл мог остаться
                legacy = self._path_for_key(str(meta.get("digest") or key))
                if not self._usable(legacy):
                    continue
                path = legacy
            out.append({
                "id": f"synth:{key}",
                "digest": key,
                "title": meta.get("text") or key,
                "text": meta.get("text") or "",
                "kind": meta.get("kind") or "greeting",
                "name": meta.get("name"),
                "variant": meta.get("variant"),
                "created": meta.get("ts"),
                "size": os.path.getsize(path),
                "path": path,
            })
        out.sort(key=lambda i: (i["kind"], i["title"].lower(), str(i.get("variant"))))
        return out

    def delete_synthesized(self, digest):
        """Удаляет файл озвучки по ключу индекса (используется саундбордом)."""
        path = self._path_for_key(digest)
        removed = False
        try:
            os.remove(path)
            removed = True
        except OSError:
            pass
        # вариант приветствия может лежать в файле «старого» формата
        with self._index_lock:
            items = self._index_load()
        meta = items.get(digest) or {}
        if not removed and meta.get("text"):
            legacy = self._path_for_key(
                hashlib.sha1(str(meta["text"]).encode("utf-8")).hexdigest()[:16])
            try:
                os.remove(legacy)
                removed = True
            except OSError:
                pass
        self._index_remove(digest)
        return removed

    # ---------- жизненный цикл ----------
    def stop(self, timeout=2.0):
        """Останавливает рабочий поток синтеза и закрывает движок TTS."""
        self._stop.set()
        worker = getattr(self, "_worker", None)
        if worker and worker.is_alive():
            worker.join(timeout=timeout)
        with self._engine_lock:
            engine, self._engine = self._engine, None
        if engine is not None:
            try:
                engine.close()
            except Exception:  # pragma: no cover
                log.exception("Не удалось закрыть TTS-движок")

    def invalidate(self, name):
        """Удаляет всю озвучку человека (все варианты пула фраз)."""
        with self._lock:
            self._pending.discard(name)
            self._errors.pop(name, None)
            self._last_variant.pop(name, None)
        for variant in self.variants_for(name):
            try:
                os.remove(variant["path"])
            except OSError:
                pass
            legacy = os.path.join(
                self.cache_dir,
                f"{hashlib.sha1(variant['text'].encode('utf-8')).hexdigest()[:16]}.wav")
            try:
                os.remove(legacy)
            except OSError:
                pass
            self._index_remove(variant["key"])
            self._index_remove(hashlib.sha1(
                variant["text"].encode("utf-8")).hexdigest()[:16])

    # ---------- внутренний синтез ----------
    def _synthesize_in_subprocess(self, text, out_path):
        """Точка расширения для тестов: синтез ``text`` в ``out_path``.

        Реализация делегирует работу выбранному движку TTS (который сам
        изолирует синтез в отдельном процессе). Текст нормализуется
        (:func:`facegate.tts.textnorm.normalize_for_tts`): дефисы, тире,
        спецсимволы и «слипшиеся» цифры — типовая причина обрезанной фразы
        у токенизатора Silero.
        """
        engine = self.get_engine()
        if engine is None:
            raise engines.TTSError(
                "TTS-движок не найден. Установите espeak-ng (sudo apt install "
                "espeak-ng) или Silero TTS: pip install silero scipy (в Docker-образе сервера уже есть).")
        engine.synthesize(normalize_for_tts(text), out_path, voice=self.voice_hint,
                          rate=self.rate, speed=self.speed, timeout=self.synth_timeout)
        if not (os.path.exists(out_path) and os.path.getsize(out_path) > 44):
            raise engines.TTSError("TTS вернул пустой файл")

    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                name, force, ev = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if self._stop.is_set():
                break
            variants = self.variants_for(name)
            todo = variants if force else [v for v in variants if not v["ready"]]
            if not todo:
                with self._lock:
                    self._pending.discard(name)
                if ev:
                    ev.set()
                continue
            failures = []
            done = 0
            for variant in todo:
                if self._stop.is_set():
                    break
                text, out_path = variant["text"], variant["path"]
                tmp_path = out_path + ".part"
                ok = False
                last_err = None
                for _attempt in range(2):
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                        self._synthesize_in_subprocess(normalize_for_tts(text), tmp_path)
                        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 44:
                            if os.path.exists(out_path):
                                os.remove(out_path)
                            os.replace(tmp_path, out_path)
                            ok = True
                            break
                        last_err = "TTS вернул пустой файл"
                        log.warning("%s для %s, повтор", last_err, name)
                    except Exception as e:  # noqa: BLE001 - ошибку пишем в статус
                        last_err = e
                        log.error("Ошибка TTS для %s (фраза «%s»): %s", name, text[:50], e)
                        if isinstance(last_err, str) and "не найден" in last_err:
                            break  # движка нет совсем — повторять бессмысленно
                if ok:
                    done += 1
                    self._index_add(variant["key"], text, kind="greeting", name=name,
                                    variant=variant["index"])
                    log.info("Озвучка готова: %s (вариант %d/%d)",
                             name, variant["index"] + 1, len(self.templates))
                else:
                    failures.append(str(last_err) if last_err else "TTS вернул пустой файл")
            with self._lock:
                self._pending.discard(name)
                if failures and done == 0:
                    self._errors[name] = failures[0]
                else:
                    self._errors.pop(name, None)
            if done:
                log.info("Озвучка для %s: готово вариантов %d из %d",
                         name, done, len(todo))
            else:
                log.error("Не удалось синтезировать фразу для %s", name)
            if ev:
                ev.set()
