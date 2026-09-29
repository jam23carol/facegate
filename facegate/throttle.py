# -*- coding: utf-8 -*-
import time


class GreetThrottle:
    """Не позволяет приветствовать одного человека чаще раза в cooldown секунд."""

    def __init__(self, cooldown):
        self.cooldown = cooldown
        self._last = {}

    def allow(self, name, now=None):
        now = time.monotonic() if now is None else now
        last = self._last.get(name)
        if last is not None and now - last < self.cooldown:
            return False
        self._last[name] = now
        return True

    def reset(self, name=None):
        if name is None:
            self._last.clear()
        else:
            self._last.pop(name, None)

    def wait_left(self, name, now=None):
        """Сколько секунд осталось до следующего разрешённого приветствия."""
        now = time.monotonic() if now is None else now
        last = self._last.get(name)
        if last is None:
            return 0.0
        return max(0.0, round(self.cooldown - (now - last), 2))

    def snapshot(self, now=None):
        """Состояние кулдаунов для админ-панели: имя -> секунд осталось."""
        now = time.monotonic() if now is None else now
        return {name: self.wait_left(name, now) for name in sorted(self._last)}
