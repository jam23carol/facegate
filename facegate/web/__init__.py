# -*- coding: utf-8 -*-
"""Веб-слой админ-панели: Flask-приложение, авторизация, HTML/CSS/JS шаблоны."""
from facegate.web.app import create_app
from facegate.web.auth import AdminAuth

__all__ = ["create_app", "AdminAuth"]
