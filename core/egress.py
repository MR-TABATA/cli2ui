"""Outbound-connection allowlist for hosted mode.

cli2ui dials whatever host a saved connection names, so on a public deployment
the connection form is a way to make the server probe networks it can reach but
you can't. In hosted mode every connection — the driver connect and the
pg_dump / mysqldump / psql child processes alike — goes through
:func:`resolve_target`, which

1. requires ``host:port`` to match ``CLI2UI_HOSTED_TARGETS`` (empty = deny all),
2. resolves the name and refuses any address that is not globally routable
   (loopback, link-local incl. cloud metadata, private ranges) unless it sits
   inside ``CLI2UI_HOSTED_PRIVATE_NETS``, and
3. returns the vetted IP so the caller connects to *that* address — a second DNS
   lookup could otherwise be answered differently (DNS rebinding).

Outside hosted mode it does nothing and returns ``None``.
This is an application-layer check, not a substitute for network rules.
"""
import fnmatch
import ipaddress
import socket

from django.conf import settings
from django.utils.translation import gettext as _


class EgressDenied(Exception):
    """The target is not allowed; the message is safe to show in the UI."""


def _entries(name):
    return [e for e in getattr(settings, name, ()) if e]


def parse_target(entry: str):
    """'db.example.com:5432' / '*.example.com:*' -> (host pattern, port|None)."""
    host, sep, port = entry.strip().rpartition(":")
    if not sep or not host:
        raise ValueError(f"{entry!r}: expected host:port")
    if port != "*" and not (port.isdigit() and 0 < int(port) < 65536):
        raise ValueError(f"{entry!r}: bad port")
    return host.lower(), None if port == "*" else int(port)


def parse_net(entry: str):
    return ipaddress.ip_network(entry.strip(), strict=False)


def _allowed_by_targets(host: str, port: int) -> bool:
    for entry in _entries("CLI2UI_HOSTED_TARGETS"):
        pattern, p = parse_target(entry)
        if (p is None or p == port) and fnmatch.fnmatchcase(host.lower(), pattern):
            return True
    return False


def _ip_ok(ip) -> bool:
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip.is_global:
        return True
    return any(ip in parse_net(n) for n in _entries("CLI2UI_HOSTED_PRIVATE_NETS")
               if parse_net(n).version == ip.version)


def resolve_target(host: str, port: int):
    """The vetted IP to connect to, or None outside hosted mode. Raises
    EgressDenied when hosted mode does not allow this target."""
    from .hosted import is_hosted
    if not is_hosted():
        return None
    host = (host or "").strip()
    port = int(port)
    if not _entries("CLI2UI_HOSTED_TARGETS"):
        raise EgressDenied(_(
            "No target databases are allowed in hosted mode. "
            "Set CLI2UI_HOSTED_TARGETS to the host:port pairs cli2ui may reach."))
    if not _allowed_by_targets(host, port):
        raise EgressDenied(_(
            "%(host)s:%(port)s is not in CLI2UI_HOSTED_TARGETS.") % {"host": host, "port": port})
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EgressDenied(_("Could not resolve %(host)s.") % {"host": host}) from exc
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not _ip_ok(ip):
            raise EgressDenied(_(
                "%(host)s resolves to %(ip)s, which is not a public address. "
                "Add its range to CLI2UI_HOSTED_PRIVATE_NETS if this is intended.")
                % {"host": host, "ip": ip})
        ips.append(str(ip))
    # Every answer was vetted; pin one. IPv4 first: a name that lists ::1 before
    # 127.0.0.1 would otherwise fail where a normal client falls back.
    return next((i for i in ips if ":" not in i), ips[0])
