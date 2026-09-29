# -*- coding: utf-8 -*-
"""Подслой распознавания: работа с face_recognition/dlib и хранилище эталонов.

Все вызовы dlib сериализуются глобальной блокировкой
:data:`facegate.recognition.engine.INFERENCE_LOCK` — это устраняет дедлок
между потоком распознавания и HTTP-воркерами админ-панели.
"""
from facegate.recognition.engine import (INFERENCE_LOCK, InferenceBusyError,
                                         crop_face, detect_faces_for_capture,
                                         detect_in_crop, detect_with_embeddings,
                                         downscale_for_analysis, embedding_from_box,
                                         encode_jpeg, face_engine, inference_lock,
                                         inference_locked, recognize_face,
                                         require_face_recognition, scale_box)
from facegate.recognition.store import IMAGE_EXTS, FaceStore, sanitize_name

__all__ = [
    "FaceStore", "sanitize_name", "IMAGE_EXTS",
    "face_engine", "require_face_recognition", "recognize_face", "scale_box",
    "downscale_for_analysis", "detect_faces_for_capture", "crop_face", "encode_jpeg",
    "detect_in_crop", "detect_with_embeddings", "embedding_from_box",
    "inference_lock", "inference_locked", "InferenceBusyError", "INFERENCE_LOCK",
]
