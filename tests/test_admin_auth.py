# -*- coding: utf-8 -*-
"""Авторизация админ-панели (admin_auth.py)."""
import json
import os

import pytest

from facegate.web.auth import (DEFAULT_PASSWORD, DEFAULT_USERNAME, AdminAuth,
                        hash_password, verify_password)


def test_default_user_created(tmp_path):
    auth = AdminAuth(data_dir=str(tmp_path))
    assert auth.users() == [DEFAULT_USERNAME]
    assert auth.verify("admin", "admin") is True
    assert auth.verify("admin", "wrong") is False
    assert auth.is_default_credentials() is True


def test_credentials_from_args(tmp_path):
    auth = AdminAuth(data_dir=str(tmp_path), username="root", password="s3cret!")
    assert auth.users() == ["root"]
    assert auth.verify("root", "s3cret!") is True
    assert auth.verify("root", "admin") is False
    assert auth.is_default_credentials() is False


def test_credentials_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USER", "envuser")
    monkeypatch.setenv("ADMIN_PASSWORD", "envpass1")
    auth = AdminAuth(data_dir=str(tmp_path))
    assert auth.verify("envuser", "envpass1") is True


def test_users_persist_between_restarts(tmp_path):
    AdminAuth(data_dir=str(tmp_path), username="admin", password="pw1234")
    auth2 = AdminAuth(data_dir=str(tmp_path))
    assert auth2.verify("admin", "pw1234") is True
    assert auth2.is_default_credentials() is False
    # файл не должен хранить пароль открытым текстом
    raw = json.loads(open(os.path.join(str(tmp_path), "admin_users.json"),
                          encoding="utf-8").read())
    assert "pw1234" not in json.dumps(raw)
    assert raw["users"]["admin"]["algo"] == "pbkdf2_sha256"


def test_change_password(tmp_path):
    auth = AdminAuth(data_dir=str(tmp_path))
    ok, msg = auth.change_password("admin", "wrong", "newpass")
    assert ok is False and "неверен" in msg
    ok, msg = auth.change_password("admin", DEFAULT_PASSWORD, DEFAULT_PASSWORD)
    assert ok is False and "совпадает" in msg
    ok, msg = auth.change_password("admin", DEFAULT_PASSWORD, "ab")
    assert ok is False and "не короче" in msg
    ok, msg = auth.change_password("admin", DEFAULT_PASSWORD, "newpass")
    assert ok is True
    assert auth.verify("admin", "newpass") is True
    assert auth.verify("admin", DEFAULT_PASSWORD) is False
    assert auth.is_default_credentials() is False


def test_change_password_unknown_user(tmp_path):
    auth = AdminAuth(data_dir=str(tmp_path))
    ok, _ = auth.change_password("ghost", "x", "yyyy")
    assert ok is False


def test_add_and_delete_user(tmp_path):
    auth = AdminAuth(data_dir=str(tmp_path))
    ok, _ = auth.add_user("second", "pass123")
    assert ok is True
    assert auth.verify("second", "pass123") is True
    ok, msg = auth.add_user("second", "other")
    assert ok is False and "уже есть" in msg
    ok, msg = auth.add_user("", "other")
    assert ok is False
    ok, _ = auth.delete_user("second")
    assert ok is True
    assert auth.users() == ["admin"]
    ok, msg = auth.delete_user("admin")
    assert ok is False and "единственного" in msg


def test_secret_key_is_stable(tmp_path):
    a = AdminAuth(data_dir=str(tmp_path))
    b = AdminAuth(data_dir=str(tmp_path))
    assert a.secret_key == b.secret_key
    assert len(a.secret_key) >= 32


def test_secret_key_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET_KEY", "fixed-secret")
    auth = AdminAuth(data_dir=str(tmp_path))
    assert auth.secret_key == b"fixed-secret"


def test_disabled_auth_accepts_everything(tmp_path):
    auth = AdminAuth(data_dir=str(tmp_path), enabled=False)
    assert auth.enabled is False
    assert auth.verify("whoever", "whatever") is True
    assert auth.users() == []


def test_bruteforce_lock(tmp_path):
    auth = AdminAuth(data_dir=str(tmp_path))
    key = "1.2.3.4|admin"
    for _ in range(8):
        assert auth.verify("admin", "bad", client_key=key) is False
    assert auth.is_locked(key) is True
    # даже с верным паролем запрос отклоняется, пока действует блокировка
    assert auth.verify("admin", DEFAULT_PASSWORD, client_key=key) is False
    assert auth.lock_info(key)["locked"] is True
    auth.reset_failures(key)
    assert auth.is_locked(key) is False
    assert auth.verify("admin", DEFAULT_PASSWORD, client_key=key) is True


def test_password_hash_is_salted():
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a["salt"] != b["salt"]
    assert a["hash"] != b["hash"]
    assert verify_password("same-password", a) is True
    assert verify_password("same-password", b) is True
    assert verify_password("other", a) is False


def test_verify_password_broken_record():
    assert verify_password("x", {}) is False
    assert verify_password("x", {"salt": "zz", "hash": "qq"}) is False


def test_info_and_must_change(tmp_path):
    auth = AdminAuth(data_dir=str(tmp_path))
    info = auth.info("admin")
    assert info["username"] == "admin" and info["must_change"] is True
    assert auth.info("ghost") is None
    auth.set_password("admin", "brandnew")
    assert auth.info("admin")["must_change"] is False
