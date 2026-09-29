# -*- coding: utf-8 -*-
"""Подсистема озвучки (TTS): движки синтеза и кэш готовых фраз."""
from facegate.tts.cache import VoiceCache, format_template
from facegate.tts.engines import (SILERO_SPEAKERS, TTSError, detect_engine,
                                  get_engine, list_available_engines)

__all__ = [
    "VoiceCache", "format_template",
    "TTSError", "get_engine", "detect_engine", "list_available_engines",
    "SILERO_SPEAKERS",
]
