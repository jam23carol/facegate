# -*- coding: utf-8 -*-
"""Авторизация в админ-панели: пользователи, хэши паролей, secret key для сессий.

Зависимостей кроме стандартной библиотеки нет: пароли хранятся как
PBKDF2-HMAC-SHA256 (соль на пользователя) в ``admin_data/admin_users.json``,
ключ сессий Flask — в ``admin_data/secret_key`` (чтобы вход не слетал после
перезапуска сервера).

Учётные данные по умолчанию: ``admin`` / ``admin``. Их можно задать через
переменные окружения ``ADMIN_USER`` / ``ADMIN_PASSWORD`` или аргументы
``--admin-user`` / ``--admin-password``. Панель показывает предупреждение,
пока пароль не сменён.
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

log = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 200_000
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
MIN_PASSWORD_LENGTH = 4
LOCK_THRESHOLD = 8          # попыток
LOCK_WINDOW = 300.0         # секунд
LOCK_DURATION = 300.0       # секунд


def hash_password(password, salt=None, iterations=PBKDF2_ITERATIONS):
    salt = salt or secrets.token_bytes(16)
    if isinstance(salt, str):
        salt = bytes.fromhex(salt)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {
        "salt": salt.hex(),
        "hash": digest.hex(),
        "iterations": iterations,
        "algo": "pbkdf2_sha256",
    }


def verify_password(password, record):
    try:
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["hash"])
        iterations = int(record.get("iterations") or PBKDF2_ITERATIONS)
    except (KeyError, ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


class AdminAuth:
    """Хранилище учётных записей администраторов."""

    def __init__(self, data_dir="admin_data", username=None, password=None,
                 enabled=True, secret_key=None, users_filename="admin_users.json",
                 secret_filename="secret_key"):
        self.enabled = bool(enabled)
        self.data_dir = data_dir
        self.users_file = os.path.join(data_dir, users_filename) if data_dir else None
        self.secret_file = os.path.join(data_dir, secret_filename) if data_dir else None
        self._lock = threading.RLock()
        self._failures = {}
        self._users = {}
        self._secret_key = secret_key
        self.created_default = False

        if self.enabled:
            if data_dir:
                os.makedirs(data_dir, exist_ok=True)
            self._load()
            if not self._users:
                user = username or os.environ.get("ADMIN_USER") or DEFAULT_USERNAME
                pwd = password or os.environ.get("ADMIN_PASSWORD") or DEFAULT_PASSWORD
                self._users[user] = self._make_record(pwd, must_change=(pwd == DEFAULT_PASSWORD))
                self.created_default = True
                self._save()
                log.warning("Создан администратор по умолчанию: %s / %s — смените пароль в панели!",
                            user, pwd)
            elif username and username not in self._users:
                self._users[username] = self._make_record(
                    password or DEFAULT_PASSWORD, must_change=False)
                self._save()
            elif password and username and username in self._users:
                # пароль передан при старте — обновляем (удобно для docker/CI)
                self._users[username] = self._make_record(password, must_change=False)
                self._save()

    # ---------- внутреннее ----------
    @staticmethod
    def _make_record(password, must_change=False):
        record = hash_password(password)
        record["created"] = time.time()
        record["updated"] = record["created"]
        record["must_change"] = bool(must_change)
        return record

    def _load(self):
        if not self.users_file or not os.path.exists(self.users_file):
            return
        try:
            with open(self.users_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            users = data.get("users") if isinstance(data, dict) else None
            if isinstance(users, dict):
                self._users = {str(k): v for k, v in users.items() if isinstance(v, dict)}
        except (OSError, ValueError) as e:
            log.error("Не удалось прочитать %s: %s", self.users_file, e)

    def _save(self):
        if not self.users_file:
            return False
        payload = {"version": 1, "users": self._users}
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.users_file)), exist_ok=True)
            tmp = self.users_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.users_file)
            try:
                os.chmod(self.users_file, 0o600)
            except OSError:
                pass
            return True
        except OSError as e:
            log.error("Не удалось сохранить %s: %s", self.users_file, e)
            return False

    # ---------- публичное API ----------
    @property
    def secret_key(self):
        with self._lock:
            if self._secret_key:
                return self._secret_key
            if self.secret_file and os.path.exists(self.secret_file):
                try:
                    with open(self.secret_file, "rb") as f:
                        data = f.read().strip()
                    if data:
                        self._secret_key = data
                        return self._secret_key
                except OSError as e:
                    log.error("Не удалось прочитать ключ сессий: %s", e)
            key = os.environ.get("ADMIN_SECRET_KEY") or secrets.token_hex(32)
            self._secret_key = key.encode("utf-8") if isinstance(key, str) else key
            if self.secret_file:
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(self.secret_file)), exist_ok=True)
                    with open(self.secret_file, "wb") as f:
                        f.write(self._secret_key)
                    os.chmod(self.secret_file, 0o600)
                except OSError as e:
                    log.error("Не удалось сохранить ключ сессий: %s", e)
            return self._secret_key

    def users(self):
        with self._lock:
            return sorted(self._users.keys())

    def has_user(self, username):
        with self._lock:
            return username in self._users

    def info(self, username):
        with self._lock:
            record = self._users.get(username)
            if not record:
                return None
            return {
                "username": username,
                "must_change": bool(record.get("must_change")),
                "updated": record.get("updated"),
                "created": record.get("created"),
            }

    def is_default_credentials(self):
        with self._lock:
            return any(r.get("must_change") for r in self._users.values())

    def verify(self, username, password, client_key=None):
        if not self.enabled:
            return True
        if not username or password is None:
            return False
        if client_key and self.is_locked(client_key):
            return False
        with self._lock:
            record = self._users.get(username)
        ok = bool(record) and verify_password(password, record)
        if client_key:
            if ok:
                self.reset_failures(client_key)
            else:
                self.register_failure(client_key)
        return ok

    def set_password(self, username, new_password):
        new_password = (new_password or "").strip()
        if len(new_password) < MIN_PASSWORD_LENGTH:
            return False, f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов"
        with self._lock:
            if username not in self._users:
                return False, "Пользователь не найден"
            self._users[username] = self._make_record(new_password, must_change=False)
            saved = self._save()
        return (True, "Пароль обновлён") if saved else (False, "Не удалось сохранить файл паролей")

    def change_password(self, username, old_password, new_password):
        with self._lock:
            record = self._users.get(username)
        if record is None:
            return False, "Пользователь не найден"
        if not verify_password(old_password or "", record):
            return False, "Текущий пароль неверен"
        if (old_password or "") == (new_password or ""):
            return False, "Новый пароль совпадает со старым"
        return self.set_password(username, new_password)

    def add_user(self, username, password):
        username = (username or "").strip()
        if not username or len(username) > 32:
            return False, "Имя пользователя: 1–32 символа"
        with self._lock:
            if username in self._users:
                return False, "Такой пользователь уже есть"
            self._users[username] = self._make_record(password, must_change=False)
            saved = self._save()
        return (True, f"Пользователь {username} создан") if saved else (False, "Ошибка записи")

    def delete_user(self, username):
        with self._lock:
            if username not in self._users:
                return False, "Пользователь не найден"
            if len(self._users) <= 1:
                return False, "Нельзя удалить единственного администратора"
            self._users.pop(username, None)
            saved = self._save()
        return (True, f"Пользователь {username} удалён") if saved else (False, "Ошибка записи")

    # ---------- защита от подбора ----------
    def register_failure(self, key):
        now = time.time()
        with self._lock:
            entries = [t for t in self._failures.get(key, []) if now - t < LOCK_WINDOW]
            entries.append(now)
            self._failures[key] = entries

    def reset_failures(self, key):
        with self._lock:
            self._failures.pop(key, None)

    def is_locked(self, key):
        now = time.time()
        with self._lock:
            entries = [t for t in self._failures.get(key, []) if now - t < LOCK_WINDOW]
            if entries:
                self._failures[key] = entries
            if len(entries) >= LOCK_THRESHOLD:
                return now - entries[-1] < LOCK_DURATION
            return False

    def lock_info(self, key):
        now = time.time()
        with self._lock:
            entries = [t for t in self._failures.get(key, []) if now - t < LOCK_WINDOW]
            left = max(0, LOCK_THRESHOLD - len(entries))
            return {"attempts_left": left, "locked": self.is_locked(key)}
