# -*- coding: utf-8 -*-
"""Процесс-воркер Silero TTS (официальный пакет ``silero``, модель v5_ru).

Запускается движком :class:`facegate.tts.engines.SileroTTSEngine`::

    python -m facegate.tts.silero_worker

Модель загружается один раз при старте, дальше воркер обслуживает запросы —
это устраняет загрузку torch/модели (несколько секунд) на каждую фразу, а
сам синтез занимает доли секунды.

Протокол (по одной строке JSON в stdin → одна строка JSON в stdout):

    старт:  {"ready": true, "model": "...", "speakers": [...]}
            или {"ready": false, "error": "..."}
    запрос: {"op": "synth", "text": "...", "out": "path.wav",
             "speaker": "baya", "speed": 1.0, "sample_rate": 24000,
             "pad_ms": 150}
    ответ:  {"ok": true, "seconds": 1.8}  |  {"ok": false, "error": "..."}
    ещё:    {"op": "ping"} → {"ok": true}      {"op": "shutdown"} → выход

Что исправлено в этой версии
----------------------------
* **Ограничение CPU**: ``torch.set_num_threads(SILERO_THREADS)`` (по умолчанию 2)
  и одно-поточный OpenMP. Без этого синтез одной фразы загружал все ядра на
  100%, отбирая время у потока распознавания и сетевого цикла ZeroMQ — панель
  «подвисала» во время озвучки.
* **Обрезание фраз**: текст нормализуется и режется на предложения
  (:mod:`facegate.tts.textnorm`), каждый фрагмент синтезируется отдельно и
  склеивается. Раньше один вызов ``apply_tts`` на тексте с дефисами/цифрами/
  спецзнаками мог вернуть тензор только до проблемного символа.
* **Обрыв концовки**: в конец добавляется тишина (``pad_ms``, по умолчанию
  150 мс) — плееры (ffplay/aplay) больше не «съедают» последний слог.
* **Диагностика**: в ответе возвращается длительность аудио, а подозрительно
  короткий результат логируется (эвристика ``looks_truncated``).

Модель и список моделей настраиваются переменной окружения ``SILERO_MODEL``
(по умолчанию ``v5_ru`` — русские голоса aidar/baya/kseniya/eugene/xenia).
Файлы модели кэшируются пакетом silero при первой загрузке (в Docker-образе
сервера они запекаются на этапе сборки — интернет в рантайме не нужен).
"""
import json
import os
import sys
import wave

# Ограничиваем нативные пулы потоков ДО импорта torch: OMP/MKL читают эти
# переменные при загрузке, и позже их изменить уже нельзя.
os.environ.setdefault("OMP_NUM_THREADS", str(os.environ.get("SILERO_THREADS", "2")))
os.environ.setdefault("MKL_NUM_THREADS", str(os.environ.get("SILERO_THREADS", "2")))
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

SILERO_MODEL_ID = os.environ.get("SILERO_MODEL", "v5_ru")
SILERO_LANGUAGE = os.environ.get("SILERO_LANGUAGE", "ru")
DEFAULT_SPEAKER = "baya"
DEFAULT_PAD_MS = 150
CHUNK_LIMIT = int(os.environ.get("SILERO_CHUNK_CHARS", "180"))


def _num_threads():
    try:
        return max(1, min(8, int(os.environ.get("SILERO_THREADS", "2"))))
    except (TypeError, ValueError):
        return 2


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def note(message):
    """Служебное сообщение в stderr (не ломает JSON-протокол в stdout)."""
    sys.stderr.write(f"[silero-worker] {message}\n")
    sys.stderr.flush()


def limit_threads():
    """Явно ограничивает пул потоков torch (см. модульную документацию)."""
    try:
        import torch

        torch.set_num_threads(_num_threads())
        setter = getattr(torch, "set_num_interop_threads", None)
        if setter is not None:
            try:
                setter(max(1, _num_threads() // 2))
            except RuntimeError:
                pass  # уже задано ранее — не критично
        note(f"torch.set_num_threads({torch.get_num_threads()})")
    except Exception as e:  # noqa: BLE001 - ограничение потоков не фатально
        note(f"не удалось ограничить потоки torch: {e}")


def retime(tensor, speed):
    """Ускоряет/замедляет речь линейным ресемплированием (немного меняет тон)."""
    import torch

    if not speed or abs(float(speed) - 1.0) < 0.01:
        return tensor
    t = tensor.detach().cpu().float().view(1, 1, -1)
    out = torch.nn.functional.interpolate(
        t, scale_factor=1.0 / max(0.05, float(speed)), mode="linear",
        align_corners=False)
    return out.view(-1)


def synthesize_text(model, speaker, text, sample_rate, speed):
    """Синтез текста по фрагментам со склейкой (устраняет обрезание фраз)."""
    import torch

    from facegate.tts.textnorm import normalize_for_tts, split_chunks

    chunks = split_chunks(normalize_for_tts(text), limit=CHUNK_LIMIT) or [text.strip()]
    pieces = []
    for chunk in chunks:
        if not chunk:
            continue
        audio = model.apply_tts(text=chunk, speaker=speaker, sample_rate=sample_rate)
        audio = retime(audio, speed)
        piece = audio.detach().cpu().float().reshape(-1)
        if piece.numel() == 0:
            note(f"пустой аудио-тензор для фрагмента: {chunk[:40]!r}")
            continue
        pieces.append(piece)
        # небольшая пауза между предложениями — речь звучит естественно
        if len(chunks) > 1:
            pieces.append(torch.zeros(int(sample_rate * 0.08)))
    if not pieces:
        raise ValueError("синтез вернул пустое аудио")
    return torch.cat(pieces)


def pad_silence(tensor, sample_rate, pad_ms):
    """Добавляет тишину в конец (плееры не обрезают последний слог)."""
    import torch

    try:
        pad_ms = max(0, int(pad_ms))
    except (TypeError, ValueError):
        pad_ms = DEFAULT_PAD_MS
    if pad_ms <= 0:
        return tensor
    return torch.cat([tensor.detach().cpu().float().reshape(-1),
                      torch.zeros(int(sample_rate * pad_ms / 1000.0))])


def write_wav(path, tensor, sample_rate):
    """Сохраняет float-тензор как 16-битный моно WAV (без torchaudio/scipy на записи)."""
    import numpy as np

    arr = tensor.detach().cpu().numpy().astype("float32").reshape(-1)
    if arr.size == 0:
        raise ValueError("синтез вернул пустой аудио-тензор")
    pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())
    return arr.size / float(sample_rate)


def handle_synth(model, speakers, req):
    text = str(req.get("text") or "").strip()
    out = req.get("out")
    speaker = str(req.get("speaker") or "").strip().lower()
    speed = float(req.get("speed") or 1.0)
    sample_rate = int(req.get("sample_rate") or 24000)
    pad_ms = req.get("pad_ms", DEFAULT_PAD_MS)
    if not text:
        raise ValueError("пустой текст")
    if not out:
        raise ValueError("не указан путь результата")
    if speaker not in speakers:
        speaker = DEFAULT_SPEAKER if DEFAULT_SPEAKER in speakers else speakers[0]

    from facegate.tts.textnorm import looks_truncated, normalize_for_tts

    try:
        fallback_rate = int(req.get("fallback_sample_rate") or 24000)
    except (TypeError, ValueError):
        fallback_rate = 24000

    try:
        audio = synthesize_text(model, speaker, text, sample_rate, speed)
    except Exception as e:  # noqa: BLE001 - модель может не поддерживать 48 кГц
        if fallback_rate and fallback_rate != sample_rate and "sample_rate" in str(e).lower():
            note(f"частота {sample_rate} Гц не поддержана моделью ({e}) — "
                 f"пробую {fallback_rate} Гц")
            sample_rate = fallback_rate
            audio = synthesize_text(model, speaker, text, sample_rate, speed)
        else:
            raise
    audio = pad_silence(audio, sample_rate, pad_ms)
    seconds = write_wav(out, audio, sample_rate)

    normalized = normalize_for_tts(text)
    if looks_truncated(normalized, seconds):
        note(f"ВНИМАНИЕ: аудио подозрительно короткое ({seconds:.2f} c) для текста "
             f"({len(normalized)} симв.): {normalized[:80]!r}")
    return {"seconds": round(seconds, 3), "sample_rate": int(sample_rate)}


def load_model():
    limit_threads()
    from silero import silero_tts

    model, _example_text = silero_tts(language=SILERO_LANGUAGE,
                                      speaker=SILERO_MODEL_ID)
    speakers = list(getattr(model, "speakers", None) or [DEFAULT_SPEAKER])
    return model, speakers


def main():
    try:
        model, speakers = load_model()
    except Exception as e:  # noqa: BLE001 - любую ошибку загрузки отдаём заказчику
        emit({"ready": False,
              "error": f"{type(e).__name__}: {e}. Проверьте, что установлены "
                       "пакеты silero/torch/scipy и модель доступна (в Docker она "
                       "запекается в образ при сборке; на хосте — ./install.sh --tts)."})
        return 1
    emit({"ready": True, "model": SILERO_MODEL_ID, "speakers": speakers,
          "threads": _num_threads()})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            emit({"ok": False, "error": "некорректный JSON запроса"})
            continue
        op = req.get("op")
        if op == "shutdown":
            break
        if op == "ping":
            emit({"ok": True, "model": SILERO_MODEL_ID})
            continue
        if op == "synth":
            try:
                info = handle_synth(model, speakers, req)
                emit({"ok": True, **info})
            except Exception as e:  # noqa: BLE001 - ошибку отдаём заказчику
                emit({"ok": False, "error": f"{type(e).__name__}: {e}"})
            continue
        emit({"ok": False, "error": f"неизвестная операция: {op}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
