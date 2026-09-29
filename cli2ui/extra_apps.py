"""Extra apps: a generic way to plug a Django app into cli2ui.

``CLI2UI_EXTRA_APPS`` (comma-separated module names) is appended to
``INSTALLED_APPS``; each app's ``urls`` module, if it has one, is included after
the core routes. Nothing here knows what the apps are. See specs/extra-apps.md.

settings.py imports this module, so it must not import Django models or
anything from ``core``.
"""
import importlib
import importlib.util
import re

from django.core.exceptions import ImproperlyConfigured

# Lower-case dotted identifiers only: a module name, never an AppConfig class
# path such as "pkg.apps.SomeConfig" (which would silently skip the urls import).
_MODULE_NAME = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")


def parse(raw: str, *, installed=()) -> tuple:
    """The validated app names from the environment value, in order, without
    duplicates and without anything already installed. Raises
    ImproperlyConfigured on a malformed or unimportable name — a typo must stop
    the server, not quietly leave the app out."""
    names = []
    for part in (raw or "").split(","):
        name = part.strip()
        if not name:
            continue
        if not _MODULE_NAME.match(name):
            raise ImproperlyConfigured(
                f"CLI2UI_EXTRA_APPS: {name!r} is not a module name "
                "(lower-case, dot-separated; not an AppConfig class path).")
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            raise ImproperlyConfigured(
                f"CLI2UI_EXTRA_APPS: module {name!r} was not found. "
                "Is it installed in this environment?")
        if name not in names and name not in installed:
            names.append(name)
    return tuple(names)


def url_patterns(names) -> list:
    """The ``include()`` for each app that has a ``urls`` module. An app without
    one is skipped; an error *inside* a urls module is not swallowed."""
    from django.urls import include, path
    patterns = []
    for name in names:
        module = f"{name}.urls"
        try:
            urlconf = importlib.import_module(module)
        except ModuleNotFoundError as exc:
            if exc.name == module:      # the app simply has no urls module
                continue
            raise
        patterns.append(path("", include(urlconf)))
    return patterns
