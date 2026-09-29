# -*- coding: utf-8 -*-
"""Нормализация текста для TTS: регрессия на «обрезанную фразу».

Silero TTS мог выдать тензор только до проблемного символа (дефис, тире,
кавычки-ёлочки, служебные знаки), из-за чего WAV получался коротким и фраза
звучала обрезанной. Здесь проверяется нормализация и разбиение на фрагменты.
"""
from facegate.tts.textnorm import looks_truncated, normalize_for_tts, split_chunks


def test_normalize_replaces_dashes_and_exotic_punctuation():
    text = normalize_for_tts("Обед — в 13:00 * срочно!")
    assert "—" not in text
    assert "*" not in text
    assert text.endswith("!")


def test_normalize_keeps_words_with_hyphen():
    # дефис ВНУТРИ слова сохраняется (кто-то, из-за) — иначе портится произношение
    assert "кто-то" in normalize_for_tts("Пусть кто-то зайдёт")


def test_normalize_removes_control_and_zero_width():
    text = normalize_for_tts("При\u2002вет\x00, \u200bмир\u00a0!")
    assert "\x00" not in text and "\u200b" not in text
    assert "  " not in text


def test_normalize_collapses_punctuation_and_adds_terminator():
    assert normalize_for_tts("Привет,,,").count(",") <= 1
    assert normalize_for_tts("Без точки").endswith(".")
    assert normalize_for_tts("Многоточие…").endswith(".")


def test_normalize_empty():
    assert normalize_for_tts("") == ""
    assert normalize_for_tts(None) == ""


def test_normalize_limits_length():
    assert len(normalize_for_tts("а" * 5000)) <= 2000


def test_split_chunks_by_sentences():
    text = "Первое предложение. Второе предложение! Третье?"
    chunks = split_chunks(text, limit=40)
    assert len(chunks) >= 2
    assert all(chunks for chunks in chunks)


def test_split_chunks_respects_limit():
    text = ("Очень длинное объявление, " * 40).strip()
    chunks = split_chunks(text, limit=120)
    assert chunks and all(len(c) <= 120 for c in chunks)
    # текст не потерялся
    assert sum(len(c) for c in chunks) >= len(text) * 0.9


def test_split_chunks_merges_short_sentences():
    chunks = split_chunks("Раз. Два. Три.", limit=180)
    assert len(chunks) == 1


def test_split_chunks_empty():
    assert split_chunks("") == []
    assert split_chunks(None) == []


def test_looks_truncated_detects_short_audio():
    text = "Здравствуйте, Михаил! Добро пожаловать в наш офис."
    assert looks_truncated(text, 0.4) is True       # 40 симв/с — явно обрезано
    assert looks_truncated(text, 3.5) is False      # нормальный темп речи
    assert looks_truncated("", 1.0) is False
    assert looks_truncated(text, 0) is False
