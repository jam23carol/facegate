# -*- coding: utf-8 -*-
"""Счётчики сервера (кадры, лица, приветствия, ошибки)."""
import threading
import time


class Stats:
    """Простые потокобезопасные счётчики."""

    FIELDS = (
        "frames_received", "frames_processed", "faces_detected",
        "recognized", "unknown", "greets_sent", "greets_no_audio",
        "greets_throttled", "commands_sent", "decode_errors",
        "snapshots_saved", "manual_announces", "soundboard_plays",
        "faces_captured",
    )

    def __init__(self):
        self._lock = threading.Lock()
        self._counters = {name: 0 for name in self.FIELDS}
        # Персональная статистика приветствий: имя человека -> сколько раз
        # сервер с ним поздоровался (с момента запуска).
        self._greets_by_person = {}
        self.started_at = time.time()

    def incr(self, name, amount=1):
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def incr_greet(self, person_name):
        """Учитывает приветствие: общий счётчик + счётчик конкретного человека."""
        key = str(person_name or "").strip() or "неизвестно"
        with self._lock:
            self._counters["greets_sent"] = self._counters.get("greets_sent", 0) + 1
            self._greets_by_person[key] = self._greets_by_person.get(key, 0) + 1

    def greet_count(self, person_name):
        """Сколько раз приветствовали конкретного человека."""
        with self._lock:
            return self._greets_by_person.get(str(person_name or "").strip(), 0)

    def greets_by_person(self):
        """Копия словаря «имя -> число приветствий»."""
        with self._lock:
            return dict(self._greets_by_person)

    def rename_person(self, old_name, new_name):
        """Переносит персональную статистику при переименовании человека."""
        old_key = str(old_name or "").strip()
        new_key = str(new_name or "").strip()
        if not old_key or not new_key or old_key == new_key:
            return
        with self._lock:
            count = self._greets_by_person.pop(old_key, 0)
            if count:
                self._greets_by_person[new_key] = (
                    self._greets_by_person.get(new_key, 0) + count)

    def forget_person(self, person_name):
        """Убирает персональную статистику при удалении человека."""
        with self._lock:
            self._greets_by_person.pop(str(person_name or "").strip(), None)

    def counters(self):
        with self._lock:
            return dict(self._counters)

    def snapshot(self):
        with self._lock:
            data = dict(self._counters)
            data["greets_by_person"] = dict(self._greets_by_person)
        data["uptime_sec"] = round(time.time() - self.started_at, 1)
        data["started_at"] = self.started_at
        return data


def human_uptime(seconds):
    seconds = int(seconds or 0)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}д {hours}ч {minutes}м"
    if hours:
        return f"{hours}ч {minutes}м"
    if minutes:
        return f"{minutes}м {secs}с"
    return f"{secs}с"
