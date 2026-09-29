# -*- coding: utf-8 -*-
import io
import wave

import numpy as np
import pytest

from facegate.recognition import engine as face_engine_mod


class FakeFaceRecognition:
    """Подменяет face_recognition для быстрых тестов без dlib."""

    def __init__(self):
        self.faces_present = True
        self.counter = 0

    def load_image_file(self, path):
        return np.zeros((8, 8, 3), dtype=np.uint8)

    def face_locations(self, img, model="hog"):
        if not self.faces_present:
            return []
        return [(0, 4, 4, 0)]

    def face_encodings(self, img, boxes=None):
        self.counter += 1
        vec = np.zeros(128, dtype=np.float32)
        vec[0] = 0.001 * self.counter
        return [vec]

    def face_distance(self, encodings, face_to_compare):
        arr = np.asarray(encodings, dtype=np.float64)
        target = np.asarray(face_to_compare, dtype=np.float64)
        return np.sqrt(np.sum((arr - target) ** 2, axis=1))


@pytest.fixture
def fake_fr(monkeypatch):
    fr = FakeFaceRecognition()
    monkeypatch.setattr(face_engine_mod, "face_recognition", fr)
    return fr


def make_wav_bytes(pcm_frames=b"\x00\x00" * 2205, rate=22050):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm_frames)
    return buf.getvalue()


class DummyVoice:
    """Заглушка VoiceCache с тем же интерфейсом: сразу пишет готовые WAV.

    Поддерживает пул фраз приветствия (``templates``) — по одному файлу на
    вариант, как настоящий кэш, — и «single-flight» синтез объявлений
    (``cached_text_wav`` / ``request_text`` / ``wait_text``), чтобы тесты
    проверяли реальное поведение панели: готовая фраза не синтезируется заново.
    """

    def __init__(self, template=None, templates=None, fail=False, **kwargs):
        import hashlib
        import os
        import tempfile
        from facegate.tts.cache import split_templates

        self._hashlib = hashlib
        self._os = os
        pool = split_templates(templates) or split_templates(template) or \
            ["Здравствуйте, {name}!"]
        self.templates = pool
        self.template = pool[0]
        self.fail = fail
        self.calls = []              # имена, для которых запрашивали синтез
        self.synth_texts = []        # реально «синтезированные» тексты (без кэша)
        self.announced = []          # тексты объявлений, ушедшие в синтез
        self.synthesized = {}        # digest -> meta (аналог index.json)
        self._text_cache = {}        # text -> wav bytes (кэш объявлений)
        self._last_variant = {}      # name -> key (анти-повтор)
        self._tmpdir = tempfile.mkdtemp(prefix="dummy_voice_")
        self.rate = None
        self.voice_hint = None
        self.engine_pref = kwargs.get("engine") or "auto"
        self.speed = kwargs.get("speed")
        self.errors = {}

    # ---------- служебное ----------
    def cleanup(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @staticmethod
    def _digest(template, name, index):
        from facegate.tts.cache import VoiceCache
        return VoiceCache.digest_for_name(template, name, index)

    def _wav_bytes(self):
        return make_wav_bytes(b"\x00\x00" * 100)

    # ---------- тексты и пути ----------
    def text_for(self, name, index=0):
        pool = self.templates
        return pool[min(max(int(index), 0), len(pool) - 1)].replace("{name}", name)

    def texts_for(self, name):
        return [t.replace("{name}", name) for t in self.templates]

    def variants_for(self, name):
        import os
        out = []
        for idx, template in enumerate(self.templates):
            text = template.replace("{name}", name)
            key = self._digest(template, name, idx)
            path = os.path.join(self._tmpdir, f"{key}.wav")
            out.append({"index": idx, "template": template, "text": text,
                        "key": key, "path": path,
                        "ready": os.path.exists(path)})
        return out

    def wav_path(self, name, index=0):
        pool = self.templates
        idx = min(max(int(index), 0), len(pool) - 1)
        return self.variants_for(name)[idx]["path"]

    def missing_variants(self, name):
        return [v for v in self.variants_for(name) if not v["ready"]]

    def variants_info(self, name):
        variants = self.variants_for(name)
        return {"total": len(variants),
                "ready": sum(1 for v in variants if v["ready"]),
                "texts": [v["text"] for v in variants]}

    # ---------- чтение ----------
    def get(self, name):
        for variant in self.variants_for(name):
            if variant["ready"]:
                try:
                    with open(variant["path"], "rb") as f:
                        return f.read()
                except OSError:
                    continue
        return None

    def get_random(self, name):
        """Случайный вариант без повторения предыдущего: (text, wav|None)."""
        import random
        ready = [v for v in self.variants_for(name) if v["ready"]]
        if not ready:
            return self.text_for(name), None
        last = self._last_variant.get(name)
        candidates = [v for v in ready if v["key"] != last] or ready
        choice = random.choice(candidates)
        with open(choice["path"], "rb") as f:
            data = f.read()
        self._last_variant[name] = choice["key"]
        return choice["text"], data

    def status(self, name):
        if self.fail:
            return "error"
        if any(v["ready"] for v in self.variants_for(name)):
            return "ready"
        return "missing"

    # ---------- синтез ----------
    def ensure(self, name, force=False, wait=False, timeout=30.0):
        self.calls.append(name)
        if self.fail:
            self.errors[name] = "dummy TTS failure"
            return "error"
        todo = self.variants_for(name) if force else self.missing_variants(name)
        for variant in todo:
            self.synth_texts.append(variant["text"])
            with open(variant["path"], "wb") as f:
                f.write(self._wav_bytes())
            self.synthesized[variant["key"]] = {
                "id": f"synth:{variant['key']}", "digest": variant["key"],
                "title": variant["text"], "text": variant["text"],
                "kind": "greeting", "name": name, "variant": variant["index"],
                "created": 0.0, "size": 200, "path": variant["path"],
            }
        return "ready"

    def invalidate(self, name):
        import os
        for variant in self.variants_for(name):
            self.synthesized.pop(variant["key"], None)
            try:
                os.remove(variant["path"])
            except OSError:
                pass
        self._last_variant.pop(name, None)

    def configure(self, template=None, rate=None, voice_hint=None,
                  engine=None, speed=None, templates=None):
        from facegate.tts.cache import split_templates
        changed = []
        if templates is not None:
            # пул фраз приоритетнее одиночного шаблона (как в настоящем кэше)
            pool = split_templates(templates)
            if pool:
                if pool != self.templates:
                    self.templates = pool
                    self._last_variant.clear()
                    changed.append("templates")
                if self.template != pool[0]:
                    self.template = pool[0]
                    changed.append("template")
        elif template is not None:
            pool = split_templates(template)
            if pool and pool != self.templates:
                self.templates = pool
                self.template = pool[0]
                self._last_variant.clear()
                changed.append("template")
        if rate is not None:
            self.rate = rate
            changed.append("rate")
        if voice_hint is not None:
            self.voice_hint = voice_hint
            changed.append("voice_hint")
        if engine is not None and engine != self.engine_pref:
            self.engine_pref = engine
            changed.append("engine")
        if speed is not None and speed != self.speed:
            self.speed = speed
            changed.append("speed")
        return changed

    def rebuild(self, names, force=True):
        for name in names:
            self.ensure(name, force=force)
        return len(list(names))

    def engine_available(self):
        return not self.fail

    def cache_info(self):
        import os
        files = [f for f in os.listdir(self._tmpdir) if f.endswith(".wav")]
        total = sum(os.path.getsize(os.path.join(self._tmpdir, f)) for f in files)
        return {"files": len(files), "bytes": total, "dir": self._tmpdir,
                "engine": "dummy", "engine_pref": self.engine_pref,
                "available_engines": {}, "rate": self.rate, "speed": self.speed,
                "voice_hint": self.voice_hint, "template": self.template,
                "templates": list(self.templates),
                "pending": 0, "errors": len(self.errors)}

    # ---------- объявления ----------
    def cached_text_wav(self, text):
        return self._text_cache.get(text)

    def request_text(self, text):
        return text

    def wait_text(self, text, timeout=0.0):
        return self.synthesize_now(text)

    def synthesize_now(self, text, timeout=None):
        """Синтез объявления: кэш проверяется ПЕРВЫМ (повторного синтеза нет)."""
        if not text:
            return None
        cached = self._text_cache.get(text)
        if cached is not None:
            return cached
        self.announced.append(text)
        if self.fail:
            return None
        data = make_wav_bytes(b"\x00\x00" * 50)
        self._text_cache[text] = data
        import os
        from facegate.tts.cache import VoiceCache
        digest = VoiceCache.digest_for_text(text)
        path = os.path.join(self._tmpdir, f"{digest}.wav")
        with open(path, "wb") as f:
            f.write(data)
        self.synthesized[digest] = {
            "id": f"synth:{digest}", "digest": digest, "title": text, "text": text,
            "kind": "announce", "name": None, "created": 0.0, "size": len(data),
            "path": path,
        }
        return data

    # ---------- саундборд ----------
    def list_synthesized(self):
        import os
        return [item for item in self.synthesized.values()
                if os.path.exists(item["path"])]

    def delete_synthesized(self, digest):
        import os
        if self.synthesized.pop(digest, None) is None:
            return False
        try:
            os.remove(os.path.join(self._tmpdir, f"{digest}.wav"))
        except OSError:
            pass
        return True

    def stop(self, timeout=2.0):
        return True


@pytest.fixture
def dummy_voice():
    v = DummyVoice()
    yield v
    v.cleanup()


@pytest.fixture
def failing_dummy_voice():
    v = DummyVoice(fail=True)
    yield v
    v.cleanup()


@pytest.fixture
def client_pair_factory(fake_fr, tmp_path):
    """Flask test_client без авторизации + хранилище эталонов."""
    from facegate.recognition.store import FaceStore
    from facegate.web.app import create_app

    made_voices = []

    def make(voice=None, **kwargs):
        faces_dir = str(tmp_path / "faces")
        store = FaceStore(faces_dir)
        v = voice or DummyVoice()
        made_voices.append(v)
        app = create_app(store, v, faces_dir, protect=False, **kwargs)
        app.config["CSRF_ENABLED"] = False
        app.config["TESTING"] = True
        return app.test_client(), store

    yield make
    for v in made_voices:
        if isinstance(v, DummyVoice):
            v.cleanup()


@pytest.fixture
def client_pair(client_pair_factory):
    return client_pair_factory()


@pytest.fixture
def full_app_factory(fake_fr, tmp_path):
    """Полный набор зависимостей админ-панели: hub, settings, stats, события, sender."""
    from facegate.commands import CommandSender
    from facegate.config import Settings
    from facegate.events import EventBus, LogRingHandler
    from facegate.frames import FrameHub
    from facegate.recognition.store import FaceStore
    from facegate.soundboard import Soundboard
    from facegate.stats import Stats
    from facegate.throttle import GreetThrottle
    from facegate.web.app import create_app

    made = []

    def make(voice=None, protect=False, with_sender=False, with_soundboard=False,
             **kwargs):
        faces_dir = str(tmp_path / "faces")
        debug_dir = str(tmp_path / "debug_frames")
        store = FaceStore(faces_dir)
        v = voice or DummyVoice()
        hub = FrameHub(jpeg_quality=70, stream_fps=30.0)
        settings = Settings()
        stats = Stats()
        events = EventBus(maxlen=50)
        logs = LogRingHandler(maxlen=50)
        throttle = GreetThrottle(settings.greet_cooldown)
        sender = None
        if with_sender:
            sender = CommandSender(port=0)
            sender.start()
        soundboard = None
        if with_soundboard:
            soundboard = Soundboard(sounds_dir=str(tmp_path / "soundboard"),
                                    state_path=str(tmp_path / "soundboard_state.json"),
                                    voice=v)
        app = create_app(store, v, faces_dir, hub=hub, settings=settings, stats=stats,
                         events=events, log_handler=logs, throttle=throttle,
                         sender=sender, debug_dir=debug_dir, protect=protect,
                         soundboard=soundboard, **kwargs)
        app.config["CSRF_ENABLED"] = False
        app.config["TESTING"] = True
        made.append((v, sender))
        return app, {
            "store": store, "voice": v, "hub": hub, "settings": settings,
            "stats": stats, "events": events, "logs": logs, "throttle": throttle,
            "sender": sender, "debug_dir": debug_dir, "faces_dir": faces_dir,
            "soundboard": soundboard,
        }

    yield make
    for v, sender in made:
        if isinstance(v, DummyVoice):
            v.cleanup()
        if sender is not None:
            sender.stop(timeout=1.0)


@pytest.fixture
def full_app(full_app_factory):
    app, deps = full_app_factory()
    return app.test_client(), deps


@pytest.fixture
def wav_bytes():
    return make_wav_bytes()


def make_frame(width=64, height=48, color=(30, 60, 90)):
    """Синтетический BGR-кадр для тестов трансляции."""
    import numpy as _np
    frame = _np.zeros((height, width, 3), dtype=_np.uint8)
    frame[:, :] = color
    return frame
