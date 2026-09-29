"""hosted-mode safety guard.

cli2ui is local-only by default. Set ``CLI2UI_HOSTED=1`` when you put it behind
a public address and this module (a) refuses to start on an unsafe
configuration and (b) switches the dangerous operations off unless you turn
them back on one by one. With ``CLI2UI_HOSTED`` unset every function here is a
no-op, so the local experience is unchanged.

See specs/hosted-mode.md.
"""
import base64
import binascii
import hmac
from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings
from django.core.checks import Error, Tags, Warning as CheckWarning, register
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.utils.translation import gettext as _, gettext_noop

DEFAULT_SECRET_KEY = "dev-insecure-key-change-me-for-anything-public"
MIN_SECRET_KEY_LENGTH = 50
MIN_BASIC_PASSWORD_LENGTH = 12
AUTH_PROXY = "proxy"
AUTH_BASIC = "basic"

# Every capability that is off in hosted mode until CLI2UI_HOSTED_ALLOW names it.
CAPABILITIES = {
    "write_sql": gettext_noop("SQL runner write mode"),
    "ddl": gettext_noop("table / column / index / schema changes"),
    "role_admin": gettext_noop("role create / alter / delete"),
    "database_admin": gettext_noop("database create / drop / rename / restore, backup restore"),
    "server_settings": gettext_noop("server settings (ALTER SYSTEM)"),
    "session_control": gettext_noop("cancel / kill sessions, replication slots"),
    "data_transfer": gettext_noop("import, dump and export of data"),
    "connection_admin": gettext_noop("add / delete saved connections"),
}

# URL name -> capability. One table gates every method on the route, so a
# forgotten `if` in a view can't leave a dangerous route open.
ROUTE_CAPABILITY = {
    "table_rename": "ddl", "table_truncate": "ddl", "table_drop": "ddl",
    "column_add": "ddl", "column_rename": "ddl", "column_drop": "ddl",
    "column_retype": "ddl", "column_set_null": "ddl", "column_set_default": "ddl",
    "index_create": "ddl", "index_drop": "ddl",
    "schema_create": "ddl", "schema_alter": "ddl", "schema_delete": "ddl",
    "role_create": "role_admin", "role_alter": "role_admin", "role_delete": "role_admin",
    "database_create": "database_admin", "database_drop": "database_admin",
    "database_rename": "database_admin", "database_restore": "database_admin",
    "backup_restore": "database_admin",
    "settings_update": "server_settings", "settings_reset": "server_settings",
    "activity_cancel": "session_control", "activity_kill": "session_control",
    "locks_cancel": "session_control", "locks_kill": "session_control",
    "slot_create": "session_control", "slot_drop": "session_control",
    "table_import": "data_transfer", "table_dump": "data_transfer",
    "table_export": "data_transfer", "database_dump": "data_transfer",
    "query_export": "data_transfer", "backup_download": "data_transfer",
    "connect": "connection_admin", "delete_connection": "connection_admin",
    "clear_connections": "connection_admin",
}


def is_hosted() -> bool:
    return bool(getattr(settings, "CLI2UI_HOSTED", False))


def allowed() -> frozenset:
    return frozenset(getattr(settings, "CLI2UI_HOSTED_ALLOW", ()))


def disabled_capabilities() -> frozenset:
    """Capabilities that are switched off right now (empty outside hosted)."""
    if not is_hosted():
        return frozenset()
    return frozenset(CAPABILITIES) - allowed()


def disabled_paths() -> list:
    """URL paths (connection pk normalised to 0) of the routes that are off, so
    the page can drop the buttons that would only lead to a 403."""
    from django.urls import NoReverseMatch, reverse
    off = disabled_capabilities()
    out = []
    for name, cap in ROUTE_CAPABILITY.items():
        if cap not in off:
            continue
        for args in ((), (0,)):
            try:
                out.append(reverse(name, args=args))
                break
            except NoReverseMatch:
                continue
    return out


# --- preflight ---------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    level: str   # "error" | "warning"
    id: str
    message: str


def _public_origin(origin: str) -> bool:
    host = urlparse(origin).hostname or ""
    return host not in ("localhost", "127.0.0.1", "::1", "")


def preflight() -> list:
    """Every problem with the current settings. Empty outside hosted mode."""
    if not is_hosted():
        return []
    f = []

    def err(id_, msg):
        f.append(Finding("error", id_, msg))

    def warn(id_, msg):
        f.append(Finding("warning", id_, msg))

    if settings.DEBUG:
        err("DEBUG", "DEBUG is on — error pages would leak tracebacks, settings and SQL. Unset DJANGO_DEBUG.")
    key = settings.SECRET_KEY or ""
    if key == DEFAULT_SECRET_KEY or key.startswith("dev-insecure") or len(key) < MIN_SECRET_KEY_LENGTH:
        err("SECRET_KEY", f"SECRET_KEY is the built-in default or shorter than {MIN_SECRET_KEY_LENGTH} chars. "
                          "Set DJANGO_SECRET_KEY to a long random value.")
    hosts = list(settings.ALLOWED_HOSTS)
    if not hosts or any(h in ("*", ".*") for h in hosts):
        err("ALLOWED_HOSTS", "ALLOWED_HOSTS is empty or '*'. Set CLI2UI_ALLOWED_HOSTS to your public host name(s).")
    origins = list(settings.CSRF_TRUSTED_ORIGINS)
    public = [o for o in origins if _public_origin(o)]
    if not public:
        err("CSRF_TRUSTED_ORIGINS", "No public origin in CSRF_TRUSTED_ORIGINS — POSTs from your real domain "
                                    "would fail. Set CLI2UI_EXTRA_CSRF_ORIGINS=https://your.host.")
    for o in public:
        if not o.startswith("https://"):
            err("CSRF_TRUSTED_ORIGINS", f"Public origin {o} is not https://.")

    auth = getattr(settings, "CLI2UI_HOSTED_AUTH", "")
    if auth not in (AUTH_PROXY, AUTH_BASIC):
        err("AUTH", "cli2ui has no login of its own. Declare how access is protected: "
                    "CLI2UI_HOSTED_AUTH=proxy (a reverse proxy / VPN in front authenticates) or =basic "
                    "(built-in HTTP Basic; also set CLI2UI_HOSTED_BASIC_USER / _PASSWORD).")
    elif auth == AUTH_BASIC:
        if not getattr(settings, "CLI2UI_HOSTED_BASIC_USER", ""):
            err("AUTH", "CLI2UI_HOSTED_AUTH=basic needs CLI2UI_HOSTED_BASIC_USER.")
        if len(getattr(settings, "CLI2UI_HOSTED_BASIC_PASSWORD", "")) < MIN_BASIC_PASSWORD_LENGTH:
            err("AUTH", f"CLI2UI_HOSTED_BASIC_PASSWORD must be at least {MIN_BASIC_PASSWORD_LENGTH} chars.")

    unknown = allowed() - set(CAPABILITIES)
    if unknown:
        err("ALLOW", "Unknown name(s) in CLI2UI_HOSTED_ALLOW: " + ", ".join(sorted(unknown))
            + ". Known: " + ", ".join(sorted(CAPABILITIES)))

    from . import egress
    for entry in getattr(settings, "CLI2UI_HOSTED_TARGETS", ()):
        try:
            egress.parse_target(entry)
        except ValueError as exc:
            err("TARGETS", f"CLI2UI_HOSTED_TARGETS: {exc}")
    for entry in getattr(settings, "CLI2UI_HOSTED_PRIVATE_NETS", ()):
        try:
            egress.parse_net(entry)
        except ValueError:
            err("PRIVATE_NETS", f"CLI2UI_HOSTED_PRIVATE_NETS: {entry!r} is not a CIDR range.")
    if not getattr(settings, "CLI2UI_HOSTED_TARGETS", ()):
        warn("TARGETS", "CLI2UI_HOSTED_TARGETS is empty — every database connection will be refused. "
                        "Set it to the host:port pairs cli2ui may reach.")

    if not settings.CSRF_COOKIE_SECURE:
        warn("CSRF_COOKIE_SECURE", "CSRF_COOKIE_SECURE is off — set CLI2UI_SECURE_COOKIES=1 when served over HTTPS.")
    if not settings.SECURE_SSL_REDIRECT and not getattr(settings, "SECURE_HSTS_SECONDS", 0):
        warn("HTTPS", "Neither SSL redirect nor HSTS is enabled. Fine if your proxy terminates TLS and enforces it.")
    return f


def enforce() -> None:
    """Refuse to start on an unsafe hosted configuration (called from wsgi/asgi)."""
    errors = [x for x in preflight() if x.level == "error"]
    if errors:
        raise ImproperlyConfigured(
            "cli2ui hosted mode refused to start:\n"
            + "\n".join(f"  [{x.id}] {x.message}" for x in errors)
            + "\nRun `python manage.py check_hosted` for the full report.")


@register(Tags.security)
def hosted_check(app_configs, **kwargs):
    out = []
    for x in preflight():
        cls = Error if x.level == "error" else CheckWarning
        out.append(cls(x.message, id=f"cli2ui_hosted.{x.id}"))
    return out


# --- request gate ------------------------------------------------------------

def _basic_ok(request) -> bool:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, password = base64.b64decode(token.strip(), validate=True).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    want_user = settings.CLI2UI_HOSTED_BASIC_USER.encode()
    want_pw = settings.CLI2UI_HOSTED_BASIC_PASSWORD.encode()
    # Evaluate both compares so timing doesn't reveal which half was wrong.
    ok_user = hmac.compare_digest(user.encode(), want_user)
    ok_pw = hmac.compare_digest(password.encode(), want_pw)
    return ok_user and ok_pw


def _capability_for(request):
    match = request.resolver_match
    if match is None:
        return None
    cap = ROUTE_CAPABILITY.get(match.url_name)
    if cap is None and match.url_name == "query_run" and request.POST.get("write"):
        cap = "write_sql"
    return cap


class HostedGuardMiddleware:
    """No-op unless CLI2UI_HOSTED=1. Then: optional HTTP Basic, and a 403 for
    any route whose capability hasn't been allowed."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if is_hosted() and getattr(settings, "CLI2UI_HOSTED_AUTH", "") == AUTH_BASIC and not _basic_ok(request):
            resp = HttpResponse("Authentication required.", status=401, content_type="text/plain")
            resp["WWW-Authenticate"] = 'Basic realm="cli2ui", charset="UTF-8"'
            return resp
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not is_hosted():
            return None
        cap = _capability_for(request)
        if cap is not None and cap not in allowed():
            return HttpResponse(
                _("Disabled in hosted mode: %(what)s. "
                  "Enable with CLI2UI_HOSTED_ALLOW=%(cap)s if you accept the risk.")
                % {"what": _(CAPABILITIES[cap]), "cap": cap},
                status=403, content_type="text/plain; charset=utf-8")
        return None
