# -*- coding: utf-8 -*-
import io
import os
import subprocess
import time
import wave

import pytest

from facegate.tts.cache import VoiceCache, format_template


def make_wav_bytes(frames=b"\x00\x00" * 100):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(frames)
    return buf.getvalue()


@pytest.fixture
def fake_synth(monkeypatch):
    """Подменяет субпроцессный синтез. Сценарии: список из
    'ok' | 'empty' | 'raise' | 'timeout', по одному на попытку."""
    def install(script):
        actions = list(script)

        def fake(self, text, out_path):
            action = actions.pop(0) if actions else "ok"
            if action == "raise":
                raise RuntimeError("tts exploded")
            if action == "timeout":
                raise subprocess.TimeoutExpired(cmd="tts", timeout=30)
            if action == "empty":
                with open(out_path, "wb") as f:
                    f.write(b"")
                return
            with open(out_path, "wb") as f:
                f.write(make_wav_bytes())

        monkeypatch.setattr(VoiceCache, "_synthesize_in_subprocess", fake)

    return install


def test_format_template():
    assert format_template("Здравствуйте, {name}!", "Анна") == "Здравствуйте, Анна!"
    assert format_template("Привет, {name}. Как дела, {name}?", "Боб") == \
        "Привет, Боб. Как дела, Боб?"
    assert format_template("Сломанный {unknown_tag}", "X") == "Сломанный {unknown_tag}"


def test_synthesize_and_cache(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path), template="Здравствуйте, {name}!")
    assert vc.status("Анна") == "missing"
    assert vc.ensure("Анна", wait=True) == "ready"
    data = vc.get("Анна")
    assert data is not None
    assert data[:4] == b"RIFF"
    assert vc.status("Анна") == "ready"


def test_cached_file_survives_restart(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path))
    vc.ensure("Анна", wait=True)
    path = vc.wav_path("Анна")
    # "перезапуск": новый объект поверх того же каталога, без синтеза
    vc2 = VoiceCache(cache_dir=str(tmp_path))
    vc2._synthesize_in_subprocess = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("не должен синтезировать повторно"))
    assert os.path.exists(path)
    assert vc2.get("Анна") is not None
    assert vc2.status("Анна") == "ready"


def test_invalidate(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path))
    vc.ensure("Анна", wait=True)
    assert vc.status("Анна") == "ready"
    vc.invalidate("Анна")
    assert vc.get("Анна") is None
    assert vc.status("Анна") == "missing"


def test_retry_after_empty_file(fake_synth, tmp_path):
    fake_synth(["empty", "ok"])
    vc = VoiceCache(cache_dir=str(tmp_path))
    assert vc.ensure("Боб", wait=True) == "ready"
    assert vc.get("Боб") is not None


def test_retry_after_timeout(fake_synth, tmp_path):
    fake_synth(["timeout", "ok"])
    vc = VoiceCache(cache_dir=str(tmp_path), synth_timeout=5)
    assert vc.ensure("Боб", wait=True) == "ready"


def test_persistent_failure_reports_error(fake_synth, tmp_path):
    fake_synth(["raise", "timeout"])
    vc = VoiceCache(cache_dir=str(tmp_path))
    assert vc.ensure("Кэрол", wait=True) == "error"
    assert vc.status("Кэрол") == "error"


def test_template_change_changes_cache_key(fake_synth, tmp_path):
    fake_synth(["ok"])
    v1 = VoiceCache(cache_dir=str(tmp_path), template="Здравствуйте, {name}!")
    v2 = VoiceCache(cache_dir=str(v1.cache_dir), template="Приветствую, {name}!")
    assert v1.wav_path("Анна") != v2.wav_path("Анна")


def test_ensure_is_idempotent_when_ready(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path))
    vc.ensure("Дэйв", wait=True)

    def must_not_run(self, text, out_path):
        raise AssertionError("повторный синтез не нужен")

    import facegate.tts.cache as mod
    saved = mod.VoiceCache._synthesize_in_subprocess
    mod.VoiceCache._synthesize_in_subprocess = must_not_run
    try:
        assert vc.ensure("Дэйв") == "ready"
    finally:
        mod.VoiceCache._synthesize_in_subprocess = saved


def test_real_tts_integration(tmp_path):
    """Реальный синтез доступным движком (silero/espeak/pyttsx3).

    Пропускается, если ни один TTS-движок в окружении не найден. Silero
    дополнительно требует ~600 МБ свободной памяти на torch-воркер — при
    нехватке памяти тест переключается на espeak (или пропускается).
    """
    from facegate.tts import engines
    from tests.test_tts_engines import memory_available_mb

    name = engines.detect_engine()
    if name == "не найден":
        pytest.skip("Ни один TTS-движок не доступен в этом окружении")
    engine_pref = name
    if name == "silero" and memory_available_mb() < 900:
        if engines.EspeakTTSEngine().available():
            engine_pref = "espeak"
        else:
            pytest.skip("Мало памяти для silero-воркера, а espeak не установлен")
    vc = VoiceCache(cache_dir=str(tmp_path), template="Здравствуйте, {name}!",
                    engine=engine_pref, synth_timeout=120)
    try:
        status = vc.ensure("Михаил", wait=True, timeout=180)
        assert status == "ready"
        data = vc.get("Михаил")
        assert data is not None and len(data) > 1000
    finally:
        vc.stop()


# ---------------------------------------------------------------------------
# Движок и индекс синтезированных звуков (для саундборда)
# ---------------------------------------------------------------------------
def test_configure_engine_switch(fake_synth, tmp_path):
    vc = VoiceCache(cache_dir=str(tmp_path))
    assert vc.engine_pref == "auto"
    changed = vc.configure(engine="espeak")
    assert "engine" in changed
    assert vc.engine_pref == "espeak"
    # повторная установка того же значения — не изменение
    assert vc.configure(engine="espeak") == []
    changed = vc.configure(speed=1.2)
    assert "speed" in changed and vc.speed == 1.2
    vc.stop()


def test_engine_resolution_is_lazy(fake_synth, tmp_path, monkeypatch):
    """Движок не создаётся до первого синтеза (важно для старта сервера)."""
    import facegate.tts.cache as cache_mod
    created = []
    monkeypatch.setattr(cache_mod.engines, "get_engine",
                        lambda pref="auto": created.append(pref) or None)
    vc = VoiceCache(cache_dir=str(tmp_path), engine="silero")
    assert created == []
    vc.ensure("Анна", wait=True)
    assert created and created[0] == "silero"      # создан лениво, при первом синтезе
    vc.stop()


def test_index_records_greetings_and_announces(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path))
    vc.ensure("Анна", wait=True)
    items = vc.list_synthesized()
    assert len(items) == 1
    assert items[0]["kind"] == "greeting"
    assert items[0]["name"] == "Анна"
    assert items[0]["title"] == "Здравствуйте, Анна!"
    assert os.path.exists(items[0]["path"])

    vc.synthesize_now("Обед через пять минут")
    items = vc.list_synthesized()
    assert len(items) == 2
    announce = [i for i in items if i["kind"] == "announce"]
    assert announce and announce[0]["title"] == "Обед через пять минут"
    vc.stop()


def test_index_survives_restart(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path))
    vc.ensure("Боб", wait=True)
    vc.stop()
    vc2 = VoiceCache(cache_dir=str(tmp_path))
    items = vc2.list_synthesized()
    assert [i["name"] for i in items] == ["Боб"]
    vc2.stop()


def test_invalidate_removes_index_entry(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path))
    vc.ensure("Влада", wait=True)
    assert len(vc.list_synthesized()) == 1
    vc.invalidate("Влада")
    assert vc.list_synthesized() == []
    vc.stop()


def test_delete_synthesized(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path))
    vc.ensure("Гарри", wait=True)
    item = vc.list_synthesized()[0]
    assert vc.delete_synthesized(item["digest"]) is True
    assert vc.list_synthesized() == []
    assert vc.delete_synthesized(item["digest"]) is False
    vc.stop()


# ---------------------------------------------------------------------------
# Вариативность приветствий: пул шаблонов, мультисинтез, ротация без повторов
# ---------------------------------------------------------------------------
POOL = ["Здравствуйте, {name}!", "Приветствую вас, {name}!", "Рад вас видеть, {name}!"]


def test_variants_synthesized_for_all_templates(fake_synth, tmp_path):
    import re

    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path), templates=POOL)
    assert vc.templates == POOL and vc.template == POOL[0]
    assert vc.status("Анна") == "missing"
    assert vc.ensure("Анна", wait=True) == "ready"

    info = vc.variants_info("Анна")
    assert info["total"] == 3 and info["ready"] == 3
    assert info["texts"] == [t.replace("{name}", "Анна") for t in POOL]
    # по одной записи в индексе на каждый вариант (их видно в «Саундборде»)
    assert len(vc.list_synthesized()) == 3
    # детерминированные имена файлов: {digest}_{index}.wav
    stems = [os.path.splitext(os.path.basename(v["path"]))[0]
             for v in vc.variants_for("Анна")]
    assert all(re.fullmatch(r"[0-9a-f]{16}_\d+", st) for st in stems)
    assert len(set(stems)) == 3
    vc.stop()


def test_variant_paths_are_deterministic(fake_synth, tmp_path):
    v1 = VoiceCache(cache_dir=str(tmp_path / "a"), templates=POOL)
    v2 = VoiceCache(cache_dir=str(tmp_path / "b"), templates=POOL)
    assert [v["key"] for v in v1.variants_for("Боб")] == \
           [v["key"] for v in v2.variants_for("Боб")]
    # другая фраза → другой ключ (старая озвучка не переиспользуется)
    v3 = VoiceCache(cache_dir=str(tmp_path / "c"), templates=["Пока, {name}!"])
    assert v3.variants_for("Боб")[0]["key"] != v1.variants_for("Боб")[0]["key"]
    v1.stop(); v2.stop(); v3.stop()


def test_get_random_rotates_without_immediate_repeat(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path), templates=POOL)
    vc.ensure("Вера", wait=True)

    seen, last = set(), None
    for _ in range(40):
        text, wav = vc.get_random("Вера")
        assert wav is not None and len(wav) > 44
        assert text in [t.replace("{name}", "Вера") for t in POOL]
        if last is not None:
            assert text != last, "фраза повторилась подряд"
        seen.add(text)
        last = text
    assert len(seen) == 3, "ротация должна задействовать все варианты пула"
    vc.stop()


def test_get_random_without_cache_returns_text_only(fake_synth, tmp_path):
    vc = VoiceCache(cache_dir=str(tmp_path), templates=POOL)
    text, wav = vc.get_random("Новый")
    assert wav is None
    assert text == POOL[0].replace("{name}", "Новый")
    vc.stop()


def test_partial_variants_are_usable(fake_synth, tmp_path):
    """Готов хотя бы один вариант — человек уже приветствуется со звуком."""
    calls = {"n": 0}

    def flaky(self, text, out_path):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("tts устал")
        with open(out_path, "wb") as f:
            f.write(make_wav_bytes())

    import facegate.tts.cache as mod
    saved = mod.VoiceCache._synthesize_in_subprocess
    mod.VoiceCache._synthesize_in_subprocess = flaky
    try:
        vc = VoiceCache(cache_dir=str(tmp_path), templates=POOL)
        vc.ensure("Гарри", wait=True)
        info = vc.variants_info("Гарри")
        assert info["ready"] >= 1 and info["ready"] < info["total"]
        assert vc.status("Гарри") == "ready"
        _text, wav = vc.get_random("Гарри")
        assert wav is not None
        vc.stop()
    finally:
        mod.VoiceCache._synthesize_in_subprocess = saved


def test_ensure_synthesizes_only_missing_variants(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path), templates=POOL[:2])
    vc.ensure("Дина", wait=True)
    assert vc.variants_info("Дина")["ready"] == 2

    # пул расширили — досинтезируются только новые фразы
    made = []

    def counting(self, text, out_path):
        made.append(text)
        with open(out_path, "wb") as f:
            f.write(make_wav_bytes())

    import facegate.tts.cache as mod
    saved = mod.VoiceCache._synthesize_in_subprocess
    mod.VoiceCache._synthesize_in_subprocess = counting
    try:
        vc.configure(templates=POOL)
        assert vc.ensure("Дина", wait=True) == "ready"
        assert made == [POOL[2].replace("{name}", "Дина")]
        assert vc.variants_info("Дина")["ready"] == 3
    finally:
        mod.VoiceCache._synthesize_in_subprocess = saved
    vc.stop()


def test_invalidate_removes_all_variants(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path), templates=POOL)
    vc.ensure("Ева", wait=True)
    assert len(vc.list_synthesized()) == 3
    vc.invalidate("Ева")
    assert vc.list_synthesized() == []
    assert vc.status("Ева") == "missing"
    assert vc.get("Ева") is None
    vc.stop()


def test_delete_synthesized_single_variant(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path), templates=POOL)
    vc.ensure("Женя", wait=True)
    item = vc.list_synthesized()[0]
    assert vc.delete_synthesized(item["digest"]) is True
    assert len(vc.list_synthesized()) == 2
    assert vc.delete_synthesized(item["digest"]) is False
    vc.stop()


# ---------------------------------------------------------------------------
# Объявления: кэш проверяется до синтеза, повторный синтез не запускается
# ---------------------------------------------------------------------------
def test_announce_cache_is_checked_before_synthesis(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path))
    assert vc.cached_text_wav("Обед") is None

    calls = []
    real = vc._synthesize_in_subprocess

    def counting(text, out_path):
        calls.append(text)
        return real(text, out_path)

    vc._synthesize_in_subprocess = counting
    assert vc.synthesize_now("Обед через пять минут") is not None
    assert vc.synthesize_now("Обед через пять минут") is not None    # второй раз
    assert len(calls) == 1, "готовая фраза не должна синтезироваться заново"
    assert vc.cached_text_wav("Обед через пять минут") is not None
    vc.stop()


def test_request_text_is_single_flight(tmp_path):
    """Параллельные запросы одной фразы не запускают два синтеза."""
    import threading

    vc = VoiceCache(cache_dir=str(tmp_path))
    started = threading.Barrier(2, timeout=5.0)
    calls = []

    def slow(self, text, out_path):
        calls.append(text)
        time.sleep(0.25)
        with open(out_path, "wb") as f:
            f.write(make_wav_bytes())

    import facegate.tts.cache as mod
    saved = mod.VoiceCache._synthesize_in_subprocess
    mod.VoiceCache._synthesize_in_subprocess = slow
    results = {}

    def worker(idx):
        started.wait()
        results[idx] = vc.wait_text("Одно и то же объявление", timeout=10.0)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)
            assert not t.is_alive()
        assert len(calls) == 1, f"фраза синтезирована {len(calls)} раз вместо одного"
        assert results[0] is not None and results[1] is not None
    finally:
        mod.VoiceCache._synthesize_in_subprocess = saved
        vc.stop()


def test_wait_text_returns_none_on_timeout(tmp_path):
    vc = VoiceCache(cache_dir=str(tmp_path))

    def slow(self, text, out_path):
        time.sleep(1.0)
        with open(out_path, "wb") as f:
            f.write(make_wav_bytes())

    import facegate.tts.cache as mod
    saved = mod.VoiceCache._synthesize_in_subprocess
    mod.VoiceCache._synthesize_in_subprocess = slow
    try:
        started = time.time()
        assert vc.wait_text("Долгая фраза", timeout=0.2) is None
        assert time.time() - started < 0.9
        # фоновый синтез при этом продолжается и результат кэшируется
        assert vc.wait_text("Долгая фраза", timeout=5.0) is not None
        assert vc.cached_text_wav("Долгая фраза") is not None
    finally:
        mod.VoiceCache._synthesize_in_subprocess = saved
        vc.stop()


def test_normalize_applied_before_synthesis(tmp_path):
    """В движок уходит нормализованный текст (защита от обрезанной фразы)."""
    vc = VoiceCache(cache_dir=str(tmp_path))
    seen = []

    def spy(self, text, out_path):
        seen.append(text)
        with open(out_path, "wb") as f:
            f.write(make_wav_bytes())

    import facegate.tts.cache as mod
    saved = mod.VoiceCache._synthesize_in_subprocess
    mod.VoiceCache._synthesize_in_subprocess = spy
    try:
        vc.synthesize_now("Обед * в 13:00 — срочно")
    finally:
        mod.VoiceCache._synthesize_in_subprocess = saved
    assert seen and "*" not in seen[0] and "—" not in seen[0]
    vc.stop()


def test_cache_info_reports_template_pool(fake_synth, tmp_path):
    fake_synth(["ok"])
    vc = VoiceCache(cache_dir=str(tmp_path), templates=POOL)
    info = vc.cache_info()
    assert info["templates"] == POOL
    assert info["variants"] == 3
    assert info["template"] == POOL[0]
    vc.stop()
