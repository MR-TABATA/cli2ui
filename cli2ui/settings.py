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
# Off by default: a DEBUG error page leaks tracebacks, settings, and SQL — and
# this tool sits right next to a database. Set DJANGO_DEBUG=1 when you're
# developing and want the rich error pages.
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    # intcomma だけのために入れている。行数が 7 桁になると、区切りが無い数字は
    # 桁を数えないと読めない ── 236 万行と 23 万行を見間違える。
    "django.contrib.humanize",
    "core",
    # Optional feature app: the planner what-if tools (scale simulation + index
    # lab), kept as a self-contained, removable unit. Remove this line to drop the
    # feature entirely — its routes and nav buttons disappear with it.
    "planner_lab",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Activates the request's language from the cookie set by the header toggle
    # (via set_language), falling back to the browser's Accept-Language. Must
    # come before CommonMiddleware, which needs an active language. No
    # SessionMiddleware is required: set_language stores the choice in a cookie
    # when there's no session, and LocaleMiddleware reads it from there.
    "django.middleware.locale.LocaleMiddleware",
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
                "django.template.context_processors.i18n",
                "core.context_processors.features",
                "core.context_processors.version",
            ],
        },
    },
]

WSGI_APPLICATION = "cli2ui.wsgi.application"

# Management DB: SQLite. Stores saved connections and (later) command history.
# The management DB (saved connections, command history, auto-backups) is a
# single SQLite file. CLI2UI_DB_PATH lets you point it elsewhere — e.g. onto a
# named Docker volume — so it persists cleanly when cli2ui rides along in an
# existing compose project (see README.NETWORKING.md). Defaults to the file in
# the project root.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("CLI2UI_DB_PATH", BASE_DIR / "db.sqlite3"),
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

# **The timezone the app displays its own timestamps in.**
#
# Django's default when this is unset is `America/Chicago`, which is wrong for
# everyone who did not choose it — and it does not stop at the app's own clock.
# The template engine converts *any* aware datetime it renders, including the
# rows this tool reads out of your database: a `timestamptz` holding
# 2026-09-04 00:10 UTC came out on screen as 2026-09-03 19:10. The number the
# database gave us was correct; the screen quietly changed it.
#
# For a tool whose whole job is to show what the database actually holds, that
# is the worst class of bug there is, so this is now explicit. UTC is the
# default because it is the one answer that is never quietly wrong: it matches
# what `psql` prints against a UTC server, and it is unambiguous when you are
# lining these timestamps up against server logs. Set `TZ` (docker compose
# passes the host's) to see them in your own.
#
# Raw database values are a separate matter and are not converted at all — see
# `{% localtime off %}` in the row grids.
TIME_ZONE = os.environ.get("TZ", "UTC")

# ISO 8601 for the app's own default-formatted datetimes, so a timestamp is
# never ambiguous about which end is the day and which is the month.
DATETIME_FORMAT = "Y-m-d H:i:s"
DATE_FORMAT = "Y-m-d"

# Internationalization. UI ships in English; the header toggle (set_language)
# switches to Japanese and persists it in a cookie. New visitors with no cookie
# get their browser's Accept-Language. Compiled catalogs live in locale/.
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("ja", "日本語")]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True

# Largest automatic safety snapshot (taken before a destructive op) we'll store
# as a blob in the management DB. Past this, the operation proceeds with a
# warning instead of bloating SQLite. Raise it for bigger objects.
CLI2UI_MAX_AUTO_BACKUP_BYTES = int(
    os.environ.get("CLI2UI_MAX_AUTO_BACKUP_BYTES", str(50 * 1024 * 1024))
)

# Total size of automatic safety snapshots kept *per connection*. When a new
# snapshot pushes the running total past this, the oldest are deleted (the most
# recent is always kept) — so db.sqlite3 can't grow without bound as you make
# repeated writes/drops. Raise it to keep deeper undo history.
CLI2UI_MAX_AUTO_BACKUP_TOTAL_BYTES = int(
    os.environ.get("CLI2UI_MAX_AUTO_BACKUP_TOTAL_BYTES", str(500 * 1024 * 1024))
)
