"""Django settings for cli2ui — a web UI over DB CLI commands.

Local-only by design: this tool is meant to run on your machine or in
Docker on your network. There is no SaaS, no external calls.
"""
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "dev-insecure-key-change-me-for-anything-public"
)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    # X-Frame-Options: DENY. There's no reason to frame cli2ui, and refusing to
    # be framed stops a clickjacking page from tricking you into clicking its
    # destructive buttons (drop schema/role/index) inside a hidden iframe.
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "cli2ui.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "cli2ui.wsgi.application"

# Management DB: SQLite. Stores saved connections and (later) command history.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# CSRF is enabled even though the tool is local-only: without it, any website
# you visit could fire a cross-origin form POST at localhost and mutate (e.g.
# DROP) your database. htmx sends the token via the X-CSRFToken header — see the
# hx-headers on <body> in base.html — so no per-form {% csrf_token %} is needed.
# Cookie-based CSRF works standalone here (no SessionMiddleware required).
CSRF_TRUSTED_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
