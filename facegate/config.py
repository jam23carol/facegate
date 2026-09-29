# -*- coding: utf-8 -*-
"""Настройки сервера, которые можно менять на лету из админ-панели.

:class:`Settings` хранит значения (с валидацией по :data:`SETTINGS_SPEC`),
умеет сохранять их в ``admin_data/settings.json`` и применять без перезапуска
сервера. Спецификация одновременно описывает форму в панели (тип, границы,
подсказка, группа), поэтому добавление новой настройки = одна запись в
:data:`SETTINGS_SPEC`.

Вариативность приветствий
-------------------------
Вместо одной строки ``greet_template`` задаётся **пул шаблонов**
``greet_templates`` (в панели — многострочное поле, одна строка = одна фраза)::

    Здравствуйте, {name}!
    Приветствую вас, {name}!
    Рад вас видеть, {name}!

Для каждого человека синтезируется WAV на **каждый** шаблон, а при
распознавании фраза выбирается случайно без повторений подряд
(:meth:`facegate.tts.cache.VoiceCache.get_random`). Поле ``greet_template``
сохранено для совместимости (CLI, старые settings.json, API) и всегда равно
первой строке пула — изменения синхронизируются в обе стороны.
"""
import json
import logging
import os
import threading

log = logging.getLogger(__name__)

DEFAULT_GREET_TEMPLATES = (
    "Здравствуйте, {name}!\n"
    "Приветствую вас, {name}!\n"
    "Рад вас видеть, {name}!"
)

# ---------- НАСТРОЙКИ, ДОСТУПНЫЕ ИЗ АДМИН-ПАНЕЛИ ----------
# type: float|int|str|text|bool|choice|nullint|nullfloat|nullstr (null = «авто»)
# text — многострочное поле (список строк, хранится как текст с \n)
# hidden: True — настройка доступна по API/CLI, но не показывается в форме
SETTINGS_SPEC = {
    "threshold": {
        "type": "float", "default": 0.6, "min": 0.05, "max": 1.0, "step": 0.01,
        "label": "Порог схожести", "group": "recognition",
        "help": "Дистанция эмбеддингов. Меньше = строже (0.45–0.55 строго, 0.6–0.7 мягко).",
    },
    "greet_cooldown": {
        "type": "float", "default": 30.0, "min": 0.0, "max": 3600.0, "step": 1.0,
        "label": "Кулдаун приветствия, сек", "group": "recognition",
        "help": "Как часто можно здороваться с одним и тем же человеком.",
    },
    "detect_model": {
        "type": "choice", "default": "hog", "choices": ["hog", "cnn"],
        "label": "Модель поиска лиц", "group": "recognition",
        "help": "hog — быстро на CPU, cnn — точнее, но требует GPU (dlib cnn).",
    },
    "process_width": {
        "type": "int", "default": 0, "min": 0, "max": 3840, "step": 10,
        "label": "Ширина кадра для анализа, px", "group": "recognition",
        "help": "0 = анализировать кадр целиком. 640–960 заметно ускоряет распознавание.",
    },
    "recognition_enabled": {
        "type": "bool", "default": True,
        "label": "Распознавание включено", "group": "recognition",
        "help": "Если выключить — сервер только транслирует видео, лица не ищет.",
    },
    "greeting_enabled": {
        "type": "bool", "default": True,
        "label": "Отправлять приветствия", "group": "recognition",
        "help": "Если выключить — лица распознаются и рисуются, но команды клиентам не уходят.",
    },
    "draw_labels": {
        "type": "bool", "default": True,
        "label": "Подписи на рамках (имя + дистанция)", "group": "recognition",
        "help": "Влияет на сохраняемые снимки и на подписи в браузере.",
    },
    "greet_templates": {
        "type": "text", "default": DEFAULT_GREET_TEMPLATES,
        "maxlen": 200, "maxlines": 12,
        "label": "Фразы приветствия (по одной на строку)", "group": "voice",
        "help": "{name} заменяется на имя человека. Для каждой строки синтезируется "
                "отдельный WAV, при распознавании фраза выбирается случайно "
                "(без повторов подряд). Смена списка перезапускает синтез озвучки.",
    },
    # Совместимость со старыми запусками/API/CLI: всегда = первая строка пула.
    "greet_template": {
        "type": "str", "default": "Здравствуйте, {name}!", "maxlen": 200,
        "label": "Шаблон фразы (устарел)", "group": "voice", "hidden": True,
        "help": "Однострочный шаблон. Используйте поле «Фразы приветствия» — "
                "это значение синхронизируется с первой строкой списка.",
    },
    "announce_wait": {
        "type": "float", "default": 6.0, "min": 0.0, "max": 60.0, "step": 0.5,
        "label": "Ждать озвучку объявления, сек", "group": "voice",
        "help": "Сколько времени панель ждёт синтез нового объявления, чтобы "
                "отправить его ОДНОЙ командой со звуком. Если фраза уже "
                "синтезирована — звук уходит мгновенно. 0 = отправить текст "
                "сразу, озвучка придёт следом.",
    },
    "tts_engine": {
        "type": "choice", "default": "auto",
        "choices": ["auto", "silero", "espeak", "pyttsx3"],
        "label": "Движок озвучки (TTS)", "group": "voice",
        "help": "silero — нейросетевой русский голос (в Docker установлен по умолчанию); "
               "espeak — системный espeak-ng; pyttsx3 — SAPI5/NSSpeech; "
               "auto — первый доступный (silero → espeak → pyttsx3).",
    },
    "tts_voice": {
        "type": "nullstr", "default": None, "maxlen": 64,
        "label": "Голос TTS", "group": "voice",
        "help": "Для silero: aidar, baya, kseniya, eugene, xenia. "
               "Для espeak: язык/голос, например ru. Пусто = голос по умолчанию (baya / ru).",
    },
    "tts_speed": {
        "type": "nullfloat", "default": None, "min": 0.5, "max": 2.0, "step": 0.05,
        "label": "Скорость речи (silero)", "group": "voice",
        "help": "0.5–2.0, пусто = 1.0. Применяется только движком silero.",
    },
    "tts_rate": {
        "type": "nullint", "default": None, "min": 50, "max": 450,
        "label": "Скорость речи, слов/мин (espeak)", "group": "voice",
        "help": "Пусто = значение по умолчанию движка. Применяется espeak/pyttsx3.",
    },
    "jpeg_quality": {
        "type": "int", "default": 80, "min": 25, "max": 95, "step": 1,
        "label": "Качество JPEG для трансляции", "group": "stream",
        "help": "Выше — красивее, но больше трафика и нагрузки на CPU.",
    },
    "stream_fps": {
        "type": "float", "default": 15.0, "min": 1.0, "max": 30.0, "step": 1.0,
        "label": "Макс. FPS трансляции в браузер", "group": "stream",
        "help": "Ограничивает частоту кодирования кадров для веб-панели.",
    },
    "stale_after": {
        "type": "float", "default": 8.0, "min": 1.0, "max": 300.0, "step": 1.0,
        "label": "Клиент считается отвалившимся через, сек", "group": "stream",
        "help": "После этого в панели показывается «нет сигнала».",
    },
    "command_stale_after": {
        "type": "float", "default": 30.0, "min": 5.0, "max": 600.0, "step": 1.0,
        "label": "Клиент доступен для команд в течение, сек", "group": "stream",
        "help": "Мягкий порог для объявлений/саундборда: короткая потеря видеокадров "
                "не должна приводить к ошибке «Нет клиентов в эфире».",
    },
    "stream_limit": {
        "type": "int", "default": 24, "min": 1, "max": 128, "step": 1,
        "label": "Максимум одновременных видеопотоков", "group": "stream",
        "help": "Каждая открытая вкладка с видео занимает один поток веб-сервера. "
                "Лимит защищает панель от исчерпания воркеров.",
    },
}

GROUPS = {
    "recognition": "Распознавание",
    "voice": "Озвучка (TTS)",
    "stream": "Трансляция видео",
}

# Настройки, изменение которых требует пересинтеза кэша озвучки
VOICE_AFFECTING = ("greet_templates", "greet_template", "tts_engine", "tts_voice",
                   "tts_speed", "tts_rate")


def split_templates(value):
    """Строка с шаблонами → список непустых строк без дубликатов (порядок сохраняется)."""
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


def first_template(value, fallback="Здравствуйте, {name}!"):
    lines = split_templates(value)
    return lines[0] if lines else fallback


class Settings:
    """Хранилище изменяемых настроек с валидацией и сохранением на диск."""

    def __init__(self, path=None, **overrides):
        self._lock = threading.RLock()
        self.path = path
        self._values = {k: spec["default"] for k, spec in SETTINGS_SPEC.items()}
        if path:
            self._load()
        for key, value in overrides.items():
            if value is None:
                continue
            self._values[key] = self._coerce(key, value, strict=False)
        # Явный одиночный шаблон (CLI --greet-template) важнее пула по умолчанию
        if overrides.get("greet_template") and not overrides.get("greet_templates"):
            self._values["greet_templates"] = str(overrides["greet_template"]).strip()
        self._sync_templates()

    # ---------- доступ ----------
    def __getattr__(self, item):
        # Вызывается только если атрибут не найден обычным путём
        values = self.__dict__.get("_values")
        if values is not None and item in values:
            return values[item]
        raise AttributeError(item)

    def get(self, key, default=None):
        with self._lock:
            return self._values.get(key, default)

    def to_dict(self):
        with self._lock:
            return dict(self._values)

    def describe(self):
        """Спецификация + текущие значения — для построения формы в панели."""
        with self._lock:
            values = dict(self._values)
        return {
            "settings": values,
            "spec": {k: {kk: vv for kk, vv in spec.items()} for k, spec in SETTINGS_SPEC.items()},
            "groups": dict(GROUPS),
            "voice_affecting": list(VOICE_AFFECTING),
        }

    def visible_settings(self):
        """Ключи настроек, которые показываются в форме панели (без hidden)."""
        return [k for k, spec in SETTINGS_SPEC.items() if not spec.get("hidden")]

    # ---------- шаблоны приветствия ----------
    @property
    def greet_template_list(self):
        """Список фраз приветствия (пул шаблонов)."""
        with self._lock:
            lines = split_templates(self._values.get("greet_templates"))
            if lines:
                return lines
            single = str(self._values.get("greet_template") or "").strip()
            return [single] if single else ["Здравствуйте, {name}!"]

    def _sync_templates(self):
        """Держит ``greet_template`` (первая строка) и ``greet_templates`` согласованными."""
        lines = split_templates(self._values.get("greet_templates"))
        single = str(self._values.get("greet_template") or "").strip()
        if lines:
            self._values["greet_template"] = lines[0]
        elif single:
            self._values["greet_templates"] = single
        else:
            self._values["greet_templates"] = SETTINGS_SPEC["greet_templates"]["default"]
            self._values["greet_template"] = first_template(
                self._values["greet_templates"])

    # ---------- валидация ----------
    @staticmethod
    def _coerce(key, value, strict=True):
        spec = SETTINGS_SPEC[key]
        t = spec["type"]
        try:
            if t == "bool":
                if isinstance(value, str):
                    return value.strip().lower() in ("1", "true", "yes", "on", "да")
                return bool(value)
            if t == "choice":
                value = str(value).strip().lower()
                if value not in spec["choices"]:
                    raise ValueError(f"допустимые значения: {', '.join(spec['choices'])}")
                return value
            if t == "text":
                lines = split_templates(value)
                if not lines:
                    raise ValueError("не может быть пустым (нужна хотя бы одна строка)")
                maxlines = spec.get("maxlines")
                if maxlines and len(lines) > maxlines:
                    raise ValueError(f"не больше {maxlines} строк")
                maxlen = spec.get("maxlen")
                if maxlen:
                    for line in lines:
                        if len(line) > maxlen:
                            raise ValueError(f"каждая строка не длиннее {maxlen} символов")
                return "\n".join(lines)
            if t in ("nullint", "nullstr", "nullfloat"):
                if value is None or (isinstance(value, str) and not value.strip()) or value == "":
                    return None
            if t in ("int", "nullint"):
                value = int(float(value))
            if t in ("float", "nullfloat"):
                value = float(value)
            if t in ("str", "nullstr"):
                value = str(value).strip()
                maxlen = spec.get("maxlen")
                if maxlen and len(value) > maxlen:
                    raise ValueError(f"не длиннее {maxlen} символов")
                if t == "str" and not value:
                    raise ValueError("не может быть пустым")
                return value
            if "min" in spec and value < spec["min"]:
                raise ValueError(f"не меньше {spec['min']}")
            if "max" in spec and value > spec["max"]:
                raise ValueError(f"не больше {spec['max']}")
            return value
        except ValueError as e:
            if strict:
                raise ValueError(f"{key}: {e}")
            return spec["default"]
        except (TypeError, KeyError):
            if strict:
                raise ValueError(f"{key}: недопустимое значение {value!r}")
            return spec["default"]

    def update(self, patch, save=True):
        """Применяет словарь изменений.

        Возвращает ``(applied_dict, errors_dict, voice_changed: list[str])``.
        Невалидные ключи/значения не ломают остальные.
        """
        applied, errors, voice_changed = {}, {}, []
        with self._lock:
            for key, value in dict(patch).items():
                if key not in SETTINGS_SPEC:
                    errors[key] = "неизвестный параметр"
                    continue
                try:
                    coerced = self._coerce(key, value)
                except ValueError as e:
                    errors[key] = str(e)
                    continue
                old = self._values.get(key)
                if old == coerced:
                    continue
                self._values[key] = coerced
                applied[key] = coerced
                if key in VOICE_AFFECTING:
                    voice_changed.append(key)
            # greet_template <-> greet_templates всегда согласованы.
            # Направление синхронизации определяется тем, какое поле меняли:
            # иначе обновление одиночного шаблона (CLI/API) затиралось бы пулом.
            # Синхронизация не попадает в applied/voice_changed, чтобы не
            # дублировать событие пересинтеза и не шуметь в ответах API.
            before_sync = dict(self._values)
            if "greet_templates" in applied:
                first = first_template(applied["greet_templates"])
                if self._values.get("greet_template") != first:
                    self._values["greet_template"] = first
            elif "greet_template" in applied:
                single = str(applied["greet_template"]).strip()
                if split_templates(self._values.get("greet_templates")) != [single]:
                    self._values["greet_templates"] = single
            else:
                self._sync_templates()
            changed_by_sync = {k for k, v in self._values.items()
                               if before_sync.get(k) != v}
            changed = bool(applied) or bool(changed_by_sync & set(VOICE_AFFECTING))
        if changed and save:
            self.save()
        return applied, errors, voice_changed

    # ---------- диск ----------
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        # Старые settings.json содержат только greet_template — переносим его
        # в пул шаблонов, чтобы поведение осталось прежним.
        if "greet_templates" not in data and "greet_template" in data:
            data["greet_templates"] = data["greet_template"]
        for key, value in data.items():
            if key in SETTINGS_SPEC:
                try:
                    self._values[key] = self._coerce(key, value)
                except ValueError:
                    log.warning("Пропущена некорректная настройка %s=%r", key, value)

    def save(self):
        if not self.path:
            return False
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
            return True
        except OSError as e:
            log.error("Не удалось сохранить настройки: %s", e)
            return False
