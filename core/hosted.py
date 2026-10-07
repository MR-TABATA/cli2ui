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
import ipaddress
import math
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings
from django.core.checks import Error, Tags, Warning as CheckWarning, register
from django.core.exceptions import ImproperlyConfigured
from django.core.cache import cache
from django.http import HttpResponse
from django.urls import Resolver404, resolve
from django.utils import translation
from django.utils.translation import gettext as _, gettext_noop

DEFAULT_SECRET_KEY = "dev-insecure-key-change-me-for-anything-public"
MIN_SECRET_KEY_LENGTH = 50
MIN_BASIC_PASSWORD_LENGTH = 12
AUTH_PROXY = "proxy"
AUTH_BASIC = "basic"
AUTH_EXTENSION = "extension"   # an installed app supplies the login (see register_authenticator)

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


# Routes that belong to apps plugged in via CLI2UI_EXTRA_APPS declare what they
# do, because this table cannot know them. See declare_capability().
_DECLARED_ROUTES: dict[str, str] = {}
_EXTRA_CAPABILITIES: dict[str, str] = {}


def declare_capability(url_name: str, capability: str, description: str | None = None) -> None:
    """Say which capability a route of an extra app needs in hosted mode.

    ``capability`` is either one of the built-in names (CAPABILITIES) or a new
    one, in which case ``description`` (a short phrase, shown in the 403 message)
    is required. The capability then behaves like the built-in ones: off in
    hosted mode until named in CLI2UI_HOSTED_ALLOW. Call it from
    ``AppConfig.ready()``.
    """
    if url_name in ROUTE_CAPABILITY:
        raise ValueError(f"{url_name!r} is a core route and already has a capability.")
    if capability not in CAPABILITIES and capability not in _EXTRA_CAPABILITIES:
        if not description:
            raise ValueError(f"A new capability {capability!r} needs a description.")
        _EXTRA_CAPABILITIES[capability] = description
    _DECLARED_ROUTES[url_name] = capability


# A route that must stay reachable without any capability being allowed — the
# login page of an authenticator, which has to work before anyone is let in. Only
# routes of apps in CLI2UI_EXTRA_APPS may be declared; a core route cannot be opened.
_OPEN_ROUTES: set[str] = set()


def declare_open_route(url_name: str) -> None:
    """Exempt a route of an extra app from capability gating (and from the
    default deny of undeclared state-changing routes). Call it from
    ``AppConfig.ready()``. This does not exempt it from authentication."""
    if url_name in ROUTE_CAPABILITY:
        raise ValueError(f"{url_name!r} is a core route and cannot be opened.")
    _OPEN_ROUTES.add(url_name)


# The one function that decides who may use cli2ui when CLI2UI_HOSTED_AUTH=extension.
# It receives the request and returns None to let it through, or a response
# (a login redirect, a 401) to stop it. It runs before every route, including its
# own login page — so it must let that page through itself. An exception stops the
# request: access fails closed.
_AUTHENTICATOR = None


def register_authenticator(func) -> None:
    """Install the request gate used by CLI2UI_HOSTED_AUTH=extension. Call it from
    ``AppConfig.ready()``. One authenticator only: a second registration is an
    error, not a silent replacement of the first."""
    global _AUTHENTICATOR
    if _AUTHENTICATOR is not None and _AUTHENTICATOR is not func:
        raise ImproperlyConfigured("An authenticator is already registered.")
    _AUTHENTICATOR = func


def authenticator():
    return _AUTHENTICATOR


# Who may do what to which saved connection. Authentication says who is calling;
# this says what they may touch. Two functions, both optional and both installed by
# an app that knows the answer:
#
#   authorizer(request, capability, connection_pk) -> bool
#       Asked after the hosted capability check passed (so it can only narrow, never
#       widen: a capability that is off for the deployment stays off for everyone).
#       ``capability`` is the route's capability name, or None for a plain read.
#       ``connection_pk`` is the saved connection a /c/<pk>/... route is about, or
#       None for a route that is not about one (creating a connection, clearing the list).
#   connection_scope(request, queryset) -> queryset
#       Narrows a list of saved connections to the ones this request may see.
#
# Not installed, or not hosted: nothing is narrowed. Installed: a False answer, or
# an exception, stops the request.
_AUTHORIZER = None
_CONNECTION_SCOPE = None


def register_authorizer(func) -> None:
    global _AUTHORIZER
    if _AUTHORIZER is not None and _AUTHORIZER is not func:
        raise ImproperlyConfigured("An authorizer is already registered.")
    _AUTHORIZER = func


def register_connection_scope(func) -> None:
    global _CONNECTION_SCOPE
    if _CONNECTION_SCOPE is not None and _CONNECTION_SCOPE is not func:
        raise ImproperlyConfigured("A connection scope is already registered.")
    _CONNECTION_SCOPE = func


def scope_connections(request, queryset):
    """The saved connections this request may see (all of them unless an app
    installed a scope and hosted mode is on)."""
    if is_hosted() and _CONNECTION_SCOPE is not None:
        return _CONNECTION_SCOPE(request, queryset)
    return queryset


def _connection_pk(request, view_kwargs):
    """The saved connection a route is about: the ``pk`` of a /c/<int:pk>/... route.
    (Every integer in the core's and the plug-ins' routes is that; any other route
    with a ``pk`` is not taken to be one.)"""
    match = request.resolver_match
    route = getattr(match, "route", "") or ""
    if route.startswith("c/<int:pk>") and "pk" in view_kwargs:
        return view_kwargs["pk"]
    return None


def all_capabilities() -> dict:
    return {**CAPABILITIES, **_EXTRA_CAPABILITIES}


def _route_capabilities() -> dict:
    return {**ROUTE_CAPABILITY, **_DECLARED_ROUTES}


def is_hosted() -> bool:
    return bool(getattr(settings, "CLI2UI_HOSTED", False))


def allowed() -> frozenset:
    return frozenset(getattr(settings, "CLI2UI_HOSTED_ALLOW", ()))


def disabled_capabilities() -> frozenset:
    """Capabilities that are switched off right now (empty outside hosted)."""
    if not is_hosted():
        return frozenset()
    return frozenset(all_capabilities()) - allowed()


def disabled_paths() -> list:
    """URL paths (connection pk normalised to 0) of the routes that are off, so
    the page can drop the buttons that would only lead to a 403."""
    from django.urls import NoReverseMatch, reverse
    off = disabled_capabilities()
    out = []
    for name, cap in _route_capabilities().items():
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
    if auth not in (AUTH_PROXY, AUTH_BASIC, AUTH_EXTENSION):
        err("AUTH", "cli2ui has no login of its own. Declare how access is protected: "
                    "CLI2UI_HOSTED_AUTH=proxy (a reverse proxy / VPN in front authenticates) or =basic "
                    "(built-in HTTP Basic; also set CLI2UI_HOSTED_BASIC_USER / _PASSWORD) or =extension "
                    "(an installed app supplies the login).")
    elif auth == AUTH_EXTENSION:
        if _AUTHENTICATOR is None:
            err("AUTH", "CLI2UI_HOSTED_AUTH=extension, but no installed app has registered a login. "
                        "Add the app to CLI2UI_EXTRA_APPS (and enable its edition).")
    elif auth == AUTH_BASIC:
        if not getattr(settings, "CLI2UI_HOSTED_BASIC_USER", ""):
            err("AUTH", "CLI2UI_HOSTED_AUTH=basic needs CLI2UI_HOSTED_BASIC_USER.")
        if len(getattr(settings, "CLI2UI_HOSTED_BASIC_PASSWORD", "")) < MIN_BASIC_PASSWORD_LENGTH:
            err("AUTH", f"CLI2UI_HOSTED_BASIC_PASSWORD must be at least {MIN_BASIC_PASSWORD_LENGTH} chars.")

    unknown = allowed() - set(all_capabilities())
    if unknown:
        err("ALLOW", "Unknown name(s) in CLI2UI_HOSTED_ALLOW: " + ", ".join(sorted(unknown))
            + ". Known: " + ", ".join(sorted(all_capabilities())))

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

    for name in ("ALL", "WRITE", "QUERY"):
        v = getattr(settings, f"CLI2UI_HOSTED_RATE_{name}", 0)
        if not isinstance(v, int) or v < 0:
            err("RATE", f"CLI2UI_HOSTED_RATE_{name} must be a whole number >= 0 (0 = off).")
    for entry in getattr(settings, "CLI2UI_HOSTED_TRUSTED_PROXIES", ()):
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            err("TRUSTED_PROXIES", f"CLI2UI_HOSTED_TRUSTED_PROXIES: {entry!r} is not an IP or CIDR range.")
    if not any(_limits().values()):
        warn("RATE", "All hosted rate limits are 0 (off). Limit request rates at your proxy instead.")

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


UNDECLARED = object()   # an extra-app route that changes state and declared nothing


def _extra_app_names() -> tuple:
    return tuple(getattr(settings, "CLI2UI_EXTRA_APPS", ()))


def _is_extra_route(match) -> bool:
    """Whether a resolved route comes from an app in CLI2UI_EXTRA_APPS: by the
    module of its view, or by the name of a route the app declared."""
    func = getattr(match, "func", None)
    module = getattr(getattr(func, "view_class", func), "__module__", "") or ""
    if any(module == n or module.startswith(n + ".") for n in _extra_app_names()):
        return True
    return match.url_name in _DECLARED_ROUTES


def _capability_for(request):
    match = request.resolver_match
    if match is None:
        return None
    if match.url_name in _OPEN_ROUTES and _is_extra_route(match):
        return None
    cap = _route_capabilities().get(match.url_name)
    if cap is None and match.url_name == "query_run" and request.POST.get("write"):
        cap = "write_sql"
    if cap is None and request.method not in SAFE_METHODS and _is_extra_route(match):
        # Default deny: this table can't know an extra app's routes, so one that
        # changes state and declared nothing stays shut until it declares.
        return UNDECLARED
    return cap


class HostedGuardMiddleware:
    """No-op unless CLI2UI_HOSTED=1. Then: optional HTTP Basic, and a 403 for
    any route whose capability hasn't been allowed."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if is_hosted() and getattr(settings, "CLI2UI_HOSTED_AUTH", "") == AUTH_EXTENSION:
            if _AUTHENTICATOR is None:      # preflight refuses this; here it stays shut regardless
                return HttpResponse("Authentication is not available.", status=503, content_type="text/plain")
            denied = _AUTHENTICATOR(request)
            if denied is not None:
                return denied
        if is_hosted() and getattr(settings, "CLI2UI_HOSTED_AUTH", "") == AUTH_BASIC and not _basic_ok(request):
            resp = HttpResponse("Authentication required.", status=401, content_type="text/plain")
            resp["WWW-Authenticate"] = 'Basic realm="cli2ui", charset="UTF-8"'
            return resp
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not is_hosted():
            return None
        cap = _capability_for(request)
        if cap is UNDECLARED:
            return HttpResponse(
                _("Disabled in hosted mode: this route belongs to an extra app and does not "
                  "declare what it needs, so it is kept shut."),
                status=403, content_type="text/plain; charset=utf-8")
        if cap is not None and cap not in allowed():
            return HttpResponse(
                _("Disabled in hosted mode: %(what)s. "
                  "Enable with CLI2UI_HOSTED_ALLOW=%(cap)s if you accept the risk.")
                % {"what": _(all_capabilities()[cap]), "cap": cap},
                status=403, content_type="text/plain; charset=utf-8")
        if _AUTHORIZER is not None:
            pk = _connection_pk(request, view_kwargs)
            # A route about no connection and needing no capability (the landing
            # page, language switch) has nothing to ask about.
            if (pk is not None or cap is not None) and not _AUTHORIZER(request, cap, pk):
                return HttpResponse(_("You do not have access to this."),
                                    status=403, content_type="text/plain; charset=utf-8")
        return None


# --- rate limiting -----------------------------------------------------------

RATE_WINDOW_SECONDS = 60
QUERY_ROUTES = {"query_run", "explain_run"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _trusted_proxies():
    return [ipaddress.ip_network(x, strict=False)
            for x in getattr(settings, "CLI2UI_HOSTED_TRUSTED_PROXIES", ())]


def client_ip(request) -> str:
    """The caller's address. X-Forwarded-For is honoured only when the direct
    peer is a configured trusted proxy, and then read from the right (each
    trusted hop appends the address it saw) — the leftmost entries are
    whatever the client chose to claim."""
    peer = request.META.get("REMOTE_ADDR", "")
    proxies = _trusted_proxies()
    try:
        ok = any(ipaddress.ip_address(peer) in n for n in proxies)
    except ValueError:
        ok = False
    if not ok:
        return peer
    hops = [h.strip() for h in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            addr = ipaddress.ip_address(hop)
        except ValueError:
            return peer
        if not any(addr in n for n in proxies):
            return str(addr)
    return peer


def _limits():
    return {
        "all": getattr(settings, "CLI2UI_HOSTED_RATE_ALL", 120),
        "write": getattr(settings, "CLI2UI_HOSTED_RATE_WRITE", 30),
        "query": getattr(settings, "CLI2UI_HOSTED_RATE_QUERY", 20),
    }


def _hit(bucket: str, ip: str, limit: int, now: float) -> int:
    """Count one request in the current fixed window; returns the new count."""
    key = f"cli2ui:rl:{bucket}:{ip}:{int(now // RATE_WINDOW_SECONDS)}"
    cache.add(key, 0, timeout=RATE_WINDOW_SECONDS + 1)
    return cache.incr(key)


class HostedRateLimitMiddleware:
    """No-op unless CLI2UI_HOSTED=1. Per-IP fixed-window limits, counted before
    authentication so password guessing is throttled too. 0 disables a bucket.

    The counters live in Django's cache (process-local by default): with several
    workers each keeps its own count, so the effective limit is per worker."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not is_hosted():
            return self.get_response(request)
        limits = _limits()
        buckets = ["all"]
        if request.method not in SAFE_METHODS:
            buckets.append("write")
        try:
            if resolve(request.path_info).url_name in QUERY_ROUTES:
                buckets.append("query")
        except Resolver404:
            pass
        ip, now = client_ip(request), time.time()
        for bucket in buckets:
            limit = limits[bucket]
            if limit and _hit(bucket, ip, limit, now) > limit:
                retry = max(1, math.ceil(RATE_WINDOW_SECONDS - now % RATE_WINDOW_SECONDS))
                # This runs before LocaleMiddleware has picked a language, so
                # choose it from the request ourselves.
                with translation.override(translation.get_language_from_request(request)):
                    text = _("Too many requests. Try again in %(seconds)s seconds.") % {"seconds": retry}
                resp = HttpResponse(text, status=429, content_type="text/plain; charset=utf-8")
                resp["Retry-After"] = str(retry)
                return resp
        return self.get_response(request)
