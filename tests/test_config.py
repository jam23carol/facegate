# -*- coding: utf-8 -*-
"""Настройки (facegate.config), статистика (facegate.stats),
события и перехват логов (facegate.events)."""
import json
import logging

from facegate.config import SETTINGS_SPEC, Settings
from facegate.events import EventBus, LogRingHandler
from facegate.stats import Stats, human_uptime


# ---------- Settings ----------
def test_defaults():
    s = Settings()
    assert s.threshold == 0.6
    assert s.greet_cooldown == 30.0
    assert s.greet_template == "Здравствуйте, {name}!"
    assert s.recognition_enabled is True
    assert s.tts_rate is None
    assert s.detect_model == "hog"


def test_overrides_in_constructor_skip_none():
    s = Settings(threshold=0.45, tts_rate=None)
    assert s.threshold == 0.45
    assert s.tts_rate is None


def test_update_validates_ranges():
    s = Settings()
    applied, errors, _ = s.update({"threshold": 5.0})
    assert applied == {} and "threshold" in errors

    applied, errors, _ = s.update({"threshold": "0.5"})
    assert applied["threshold"] == 0.5 and not errors

    applied, errors, _ = s.update({"greet_cooldown": -1})
    assert "greet_cooldown" in errors


def test_update_unknown_key_and_choice():
    s = Settings()
    applied, errors, _ = s.update({"nope": 1})
    assert "nope" in errors
    applied, errors, _ = s.update({"detect_model": "cnn"})
    assert applied["detect_model"] == "cnn"
    applied, errors, _ = s.update({"detect_model": "magic"})
    assert "detect_model" in errors


def test_update_reports_voice_affecting():
    s = Settings()
    _applied, _errors, voice_changed = s.update({"greet_template": "Привет, {name}!"})
    assert voice_changed == ["greet_template"]
    _applied, _errors, voice_changed = s.update({"threshold": 0.55})
    assert voice_changed == []


def test_bool_coercion_from_strings():
    s = Settings()
    applied, _e, _v = s.update({"recognition_enabled": "false"})
    assert applied["recognition_enabled"] is False
    applied, _e, _v = s.update({"recognition_enabled": "yes"})
    assert applied["recognition_enabled"] is True
    applied, _e, _v = s.update({"greeting_enabled": "0"})
    assert applied["greeting_enabled"] is False
    applied, _e, _v = s.update({"greeting_enabled": 1})
    assert applied["greeting_enabled"] is True


def test_nullint_accepts_empty():
    s = Settings(tts_rate=150)
    applied, errors, _ = s.update({"tts_rate": ""})
    assert applied["tts_rate"] is None and not errors


def test_same_value_not_applied():
    s = Settings()
    applied, errors, voice = s.update({"threshold": 0.6})
    assert applied == {} and errors == {} and voice == []


def test_save_and_load_roundtrip(tmp_path):
    path = str(tmp_path / "admin_data" / "settings.json")
    s = Settings(path=path)
    s.update({"threshold": 0.42, "stream_fps": 5.0, "greet_template": "Привет, {name}!"})
    assert json.loads(open(path, encoding="utf-8").read())["threshold"] == 0.42
    s2 = Settings(path=path)
    assert s2.threshold == 0.42
    assert s2.stream_fps == 5.0
    assert s2.greet_template == "Привет, {name}!"


def test_load_ignores_broken_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{не json", encoding="utf-8")
    s = Settings(path=str(path))
    assert s.threshold == SETTINGS_SPEC["threshold"]["default"]


def test_load_skips_invalid_values(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"threshold": 99, "detect_model": "nope",
                                "stream_fps": 7}), encoding="utf-8")
    s = Settings(path=str(path))
    assert s.threshold == 0.6
    assert s.detect_model == "hog"
    assert s.stream_fps == 7


def test_describe_has_all_fields():
    s = Settings()
    desc = s.describe()
    assert set(desc["settings"]) == set(SETTINGS_SPEC)
    assert "recognition" in desc["groups"]
    assert desc["spec"]["threshold"]["min"] == 0.05


def test_to_dict_is_copy():
    s = Settings()
    d = s.to_dict()
    d["threshold"] = 0.01
    assert s.threshold == 0.6


# ---------- Stats ----------
def test_stats_counters():
    st = Stats()
    st.incr("frames_received")
    st.incr("frames_received", 4)
    st.incr("greets_sent")
    snap = st.snapshot()
    assert snap["frames_received"] == 5
    assert snap["greets_sent"] == 1
    assert snap["uptime_sec"] >= 0


# ---------- EventBus ----------
def test_event_bus_since():
    bus = EventBus(maxlen=5)
    for i in range(3):
        bus.push("greet", name=f"n{i}")
    items, nxt = bus.since(0)
    assert len(items) == 3 and nxt == 3
    bus.push("unknown")
    items2, nxt2 = bus.since(nxt)
    assert len(items2) == 1 and items2[0]["type"] == "unknown"
    assert nxt2 == 4
    assert bus.since(100)[0] == []


def test_event_bus_ring_overflow():
    bus = EventBus(maxlen=3)
    for i in range(10):
        bus.push("x", i=i)
    assert len(bus) == 3
    items, _ = bus.since(0)
    assert [i["i"] for i in items] == [7, 8, 9]


# ---------- LogRingHandler ----------
def test_log_ring_handler_captures_records():
    handler = LogRingHandler(maxlen=10)
    logger = logging.getLogger("test.log.ring")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.info("привет %s", "мир")
    logger.error("ошибка")
    lines, nxt = handler.since(0)
    assert [l["message"] for l in lines] == ["привет мир", "ошибка"]
    assert [l["level"] for l in lines] == ["INFO", "ERROR"]
    assert handler.since(nxt)[0] == []


def test_human_uptime():
    assert human_uptime(5) == "5с"
    assert human_uptime(65).startswith("1м")
    assert human_uptime(3725).startswith("1ч")
    assert human_uptime(90000).startswith("1д")

# ---------- новые настройки TTS ----------
def test_tts_engine_choice():
    s = Settings()
    assert s.tts_engine == "auto"
    applied, errors, voice = s.update({"tts_engine": "silero"})
    assert applied["tts_engine"] == "silero" and not errors
    assert "tts_engine" in voice                     # смена движка → пересинтез
    applied, errors, _ = s.update({"tts_engine": "magic"})
    assert "tts_engine" in errors


def test_tts_speed_nullfloat():
    s = Settings(tts_speed=1.25)
    assert s.tts_speed == 1.25
    applied, errors, _ = s.update({"tts_speed": ""})
    assert applied["tts_speed"] is None and not errors
    applied, errors, voice = s.update({"tts_speed": 1.5})
    assert applied["tts_speed"] == 1.5
    assert "tts_speed" in voice
    applied, errors, _ = s.update({"tts_speed": 5})
    assert "tts_speed" in errors
    applied, errors, _ = s.update({"tts_speed": 0.1})
    assert "tts_speed" in errors


def test_voice_affecting_covers_all_tts_fields():
    from facegate.config import VOICE_AFFECTING
    for key in ("greet_template", "tts_engine", "tts_voice", "tts_speed", "tts_rate"):
        assert key in VOICE_AFFECTING


def test_settings_spec_has_tts_group():
    for key in ("tts_engine", "tts_voice", "tts_speed", "tts_rate"):
        assert SETTINGS_SPEC[key]["group"] == "voice"


# ---------------------------------------------------------------------------
# Пул фраз приветствия (greet_templates) и новые настройки потоков/команд
# ---------------------------------------------------------------------------
def test_default_template_pool():
    s = Settings()
    pool = s.greet_template_list
    assert len(pool) == 3
    assert all("{name}" in t for t in pool)
    # одиночный шаблон всегда равен первой строке пула
    assert s.greet_template == pool[0]


def test_update_templates_syncs_single_template():
    s = Settings()
    applied, errors, voice_changed = s.update(
        {"greet_templates": "Добрый день, {name}!\nПривет, {name}!"})
    assert not errors and applied["greet_templates"].count("\n") == 1
    assert s.greet_template == "Добрый день, {name}!"
    assert s.greet_template_list == ["Добрый день, {name}!", "Привет, {name}!"]
    assert "greet_templates" in voice_changed


def test_update_single_template_syncs_pool():
    """Обратная совместимость: CLI/API с одной строкой обновляет и пул."""
    s = Settings()
    applied, errors, voice_changed = s.update({"greet_template": "Привет, {name}!"})
    assert not errors and applied == {"greet_template": "Привет, {name}!"}
    assert s.greet_template_list == ["Привет, {name}!"]
    assert voice_changed == ["greet_template"]


def test_templates_strip_empty_lines_and_duplicates():
    s = Settings()
    s.update({"greet_templates": "  Привет, {name}!  \n\n\nПривет, {name}!\nПока, {name}!\n"})
    assert s.greet_template_list == ["Привет, {name}!", "Пока, {name}!"]


def test_templates_accept_literal_newline_escape():
    s = Settings()
    s.update({"greet_templates": "Раз, {name}!\\nДва, {name}!"})
    assert s.greet_template_list == ["Раз, {name}!", "Два, {name}!"]


def test_templates_validation():
    s = Settings()
    _applied, errors, _ = s.update({"greet_templates": "   \n  \n"})
    assert "greet_templates" in errors
    _applied, errors, _ = s.update({"greet_templates": "\n".join(
        f"Фраза {i}, {{name}}!" for i in range(40))})
    assert "greet_templates" in errors
    _applied, errors, _ = s.update({"greet_templates": "ы" * 400})
    assert "greet_templates" in errors
    # валидное значение проходит
    applied, errors, _ = s.update({"greet_templates": "Здравия желаю, {name}!"})
    assert not errors and applied


def test_constructor_cli_template_overrides_pool():
    s = Settings(greet_template="Салют, {name}!")
    assert s.greet_template_list == ["Салют, {name}!"]
    s2 = Settings(greet_templates="A, {name}!\nB, {name}!", greet_template="C, {name}!")
    # явно заданный пул приоритетнее
    assert s2.greet_template_list == ["A, {name}!", "B, {name}!"]
    assert s2.greet_template == "A, {name}!"


def test_legacy_settings_file_migrates_single_template(tmp_path):
    import json

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"greet_template": "Старая фраза, {name}!"}),
                    encoding="utf-8")
    s = Settings(path=str(path))
    assert s.greet_template == "Старая фраза, {name}!"
    assert s.greet_template_list == ["Старая фраза, {name}!"]


def test_new_stream_and_voice_settings_have_defaults():
    s = Settings()
    assert s.command_stale_after >= s.stale_after
    assert 0 <= s.announce_wait <= 60
    assert s.stream_limit >= 1
    for key in ("command_stale_after", "stream_limit"):
        assert SETTINGS_SPEC[key]["group"] == "stream"
    for key in ("greet_templates", "announce_wait"):
        assert SETTINGS_SPEC[key]["group"] == "voice"
    from facegate.config import VOICE_AFFECTING
    assert "greet_templates" in VOICE_AFFECTING


def test_legacy_greet_template_hidden_from_form():
    """В форме панели показывается пул фраз, а не устаревшее одиночное поле."""
    s = Settings()
    visible = s.visible_settings()
    assert "greet_templates" in visible
    assert "greet_template" not in visible
    assert SETTINGS_SPEC["greet_template"]["hidden"] is True


def test_describe_includes_text_type_spec():
    s = Settings()
    desc = s.describe()
    spec = desc["spec"]["greet_templates"]
    assert spec["type"] == "text" and spec["maxlines"] == 12
    assert "\n" in desc["settings"]["greet_templates"]
