# -*- coding: utf-8 -*-
"""Нормализация текста перед синтезом речи и разбиение на фрагменты.

Зачем это нужно
---------------
Токенизатор Silero TTS «спотыкается» о нестандартную пунктуацию: длинные
тире, кавычки-ёлочки, многоточия, служебные символы (``*``, ``#``, ``|``,
``~``), слипшиеся цифры и дефисы на границах слов. В таких местах модель может
выдать тензор **только до проблемного символа** — в результате WAV получается
коротким, а фраза звучит обрезанной.

:func:`normalize_for_tts` приводит текст к «разговорному» виду, который
движки синтезируют стабильно:

* служебные/управляющие символы убираются;
* тире и дефисы, стоящие как отдельные слова, превращаются в запятые
  (пауза вместо обрезанной фразы), дефисы внутри слов сохраняются;
* кавычки, скобки и прочая «письменная» пунктуация заменяется на запятые
  или удаляется;
* повторяющиеся знаки препинания и пробелы схлопываются;
* в конце гарантированно стоит завершающий знак (``.``/``!``/``?``).

:func:`split_chunks` режет текст на предложения длиной не больше ``limit``
символов — воркер синтезирует их по очереди и склеивает аудио, поэтому длинное
объявление не обрезается и не требует от модели одного прохода на 1000 знаков.

Модуль не зависит от torch/silero и используется и сервером (кэш озвучки),
и воркером Silero.
"""
import re
import unicodedata

# управляющие и «невидимые» символы (включая zero-width)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\ufeff]")

# символы, которые заменяем паузой (запятой)
_PAUSE_CHARS = "•·*#|~/\\^&+=<>{}[]«»\"“”‘’'`"

# «письменная» пунктуация, которую безопаснее заменить пробелом
_SPACE_CHARS = "@$:;№"

_DASH_WORD_RE = re.compile(r"(?<=\s)[\u2010-\u2015_-]{1,3}(?=\s)")
_MULTI_SP_RE = re.compile(r"[ \t\u00a0]+")
_MULTI_COMMA_RE = re.compile(r"(?:\s*,){2,}")
_MULTI_DOT_RE = re.compile(r"\.{2,}")
_PUNCT_BEFORE_RE = re.compile(r"\s+([,.!?])")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")

MAX_TEXT_LEN = 2000


def normalize_for_tts(text, keep_newlines=False):
    """Приводит текст к виду, который TTS-движки синтезируют без обрезания."""
    if text is None:
        return ""
    text = unicodedata.normalize("NFC", str(text))
    text = _CONTROL_RE.sub("", text)
    if not keep_newlines:
        text = text.replace("\r", " ").replace("\n", " ")

    # многоточие → точка (иначе токенизатор «теряет» остаток фразы)
    text = text.replace("…", ".").replace("....", ".")
    # длинные тире между словами → запятая (пауза), дефисы в словах остаются
    text = _DASH_WORD_RE.sub(",", text)
    text = text.replace("—", ",").replace("–", ",").replace("−", "-")

    out = []
    for ch in text:
        if ch in _PAUSE_CHARS:
            out.append(",")
        elif ch in _SPACE_CHARS:
            out.append(" ")
        elif ch in "()":
            out.append(",")
        else:
            out.append(ch)
    text = "".join(out)

    text = _MULTI_DOT_RE.sub(".", text)
    text = _MULTI_COMMA_RE.sub(",", text)
    text = text.replace("!?", "!").replace("?,", "?").replace(".,", ".").replace("!,", "!")
    text = _PUNCT_BEFORE_RE.sub(r"\1", text)
    text = _MULTI_SP_RE.sub(" ", text).strip()
    text = re.sub(r"^[,.\s]+", "", text)

    if text and text[-1] not in ".!?":
        text += "."
    return text[:MAX_TEXT_LEN].strip()


def split_chunks(text, limit=180):
    """Режет текст на фрагменты по границам предложений (не длиннее ``limit``).

    Возвращает список непустых строк. Если предложение длиннее лимита — оно
    делится по запятым/пробелам, чтобы фрагмент гарантированно влез.
    """
    text = (text or "").strip()
    if not text:
        return []
    limit = max(40, int(limit))
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    chunks = []
    for sentence in sentences:
        if len(sentence) <= limit:
            chunks.append(sentence)
            continue
        parts = [p.strip() for p in re.split(r"(?<=[,;])\s+", sentence) if p.strip()]
        for part in parts:
            while len(part) > limit:
                cut = part.rfind(" ", 0, limit)
                if cut <= 0:
                    cut = limit
                chunks.append(part[:cut].strip())
                part = part[cut:].strip()
            if part:
                chunks.append(part)
    # склеиваем короткие соседние фрагменты — меньше швов в аудио
    merged = []
    for chunk in chunks:
        if merged and len(merged[-1]) + len(chunk) + 1 <= limit:
            merged[-1] = f"{merged[-1]} {chunk}"
        else:
            merged.append(chunk)
    return merged


def looks_truncated(text, wav_seconds):
    """Эвристика «фраза подозрительно короткая» (для диагностики в логе).

    Нормальная русская речь — примерно 12–18 символов в секунду. Если на
    текст в 60 символов получился WAV короче секунды — модель обрезала фразу.
    """
    letters = sum(1 for ch in (text or "") if ch.isalpha())
    if not letters or not wav_seconds:
        return False
    return (letters / max(float(wav_seconds), 0.01)) > 40.0
