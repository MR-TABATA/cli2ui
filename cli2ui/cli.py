"""Command-line entrypoint for running cli2ui via `uvx cli2ui` or `pipx run cli2ui`."""
import argparse
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

from cli2ui import __version__


def get_default_data_dir() -> Path:
    """Return an OS-idiomatic directory for storing cli2ui's database and keys.

    - macOS:   ~/Library/Application Support/cli2ui
    - Windows: %APPDATA%/cli2ui
    - Linux:   $XDG_DATA_HOME/cli2ui (or ~/.local/share/cli2ui)
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg_data) if xdg_data else Path.home() / ".local" / "share"
    return base / "cli2ui"


def find_available_port(host: str, start_port: int, max_attempts: int = 10) -> Optional[int]:
    """Find an available TCP port starting from start_port, supporting both IPv4 and IPv6."""
    for port in range(start_port, start_port + max_attempts):
        try:
            addrinfo = socket.getaddrinfo(
                host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
            )
        except socket.gaierror:
            return None

        for family, socktype, proto, _, sockaddr in addrinfo:
            try:
                with socket.socket(family, socktype, proto) as sock:
                    sock.bind(sockaddr)
                    return port
            except OSError:
                break
    return None


def open_browser_when_ready(check_url: str, open_url: str, timeout_seconds: float = 3.0) -> None:
    """Poll until the server starts responding, then open the browser."""
    def _poll_and_open():
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                req = urllib.request.Request(check_url, method="HEAD")
                with urllib.request.urlopen(req, timeout=0.5):
                    webbrowser.open(open_url)
                    return
            except Exception:
                time.sleep(0.15)
        # Fallback: if server took longer but didn't crash, still attempt open
        webbrowser.open(open_url)

    thread = threading.Thread(target=_poll_and_open, daemon=True)
    thread.start()


def format_url(host: str, port: int) -> str:
    """Format HTTP URL handling IPv6 addresses with brackets."""
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    if ":" in display_host and not display_host.startswith("["):
        return f"http://[{display_host}]:{port}/"
    return f"http://{display_host}:{port}/"


def normalize_csrf_origins(host: str, port: int, existing_origins: str) -> str:
    """Merge and normalize CSRF trusted origins."""
    origins = set()
    if existing_origins:
        for item in existing_origins.split(","):
            item = item.strip()
            if item:
                origins.add(item)

    # Local host variants
    origins.add(f"http://127.0.0.1:{port}")
    origins.add(f"http://localhost:{port}")

    if host not in ("0.0.0.0", "127.0.0.1", "localhost"):
        if ":" in host and not host.startswith("["):
            origins.add(f"http://[{host}]:{port}")
        else:
            origins.add(f"http://{host}:{port}")

    return ",".join(sorted(origins))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cli2ui",
        description="Launch the local web UI for PostgreSQL & MySQL commands.",
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host address to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to listen on (default: 8000)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Directory to store db.sqlite3 and secret.key (default: OS app data folder)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser automatically after starting",
    )
    parser.add_argument(
        "--edition",
        choices=["community", "pro"],
        default=os.environ.get("CLI2UI_EDITION", "community"),
        help="Feature edition to run (default: community)",
    )

    args = parser.parse_args(argv)

    # Ensure package root is in sys.path so core and planner_lab can be imported in all contexts
    pkg_dir = Path(__file__).resolve().parent
    for p in (str(pkg_dir), str(pkg_dir.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)

    # Determine and create data directory
    data_dir = args.data_dir.resolve() if args.data_dir else get_default_data_dir()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(f"Error: failed to create data directory '{data_dir}': {exc}\n")
        return 1

    db_path = data_dir / "db.sqlite3"
    secret_key_file = data_dir / "secret.key"

    # Configure environment variables before Django settings load
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cli2ui.settings")
    os.environ["CLI2UI_DB_PATH"] = str(db_path)
    os.environ["CLI2UI_SECRET_KEY_FILE"] = str(secret_key_file)
    os.environ["CLI2UI_EDITION"] = args.edition

    # Check and select port
    port = find_available_port(args.host, args.port)
    if port is None:
        sys.stderr.write(
            f"Error: all ports in range {args.port}-{args.port + 9} on host '{args.host}' are already in use.\n"
        )
        return 1

    if port != args.port:
        print(f"Port {args.port} was busy; using port {port} instead.")

    # Configure CSRF trusted origins cleanly
    existing_csrf = os.environ.get("CLI2UI_EXTRA_CSRF_ORIGINS", "")
    os.environ["CLI2UI_EXTRA_CSRF_ORIGINS"] = normalize_csrf_origins(args.host, port, existing_csrf)

    # Initialize Django
    import django
    try:
        django.setup()
    except Exception as exc:
        sys.stderr.write(f"Error: failed to initialize Django settings: {exc}\n")
        return 1

    from django.core.management import call_command

    print(f"cli2ui v{__version__}")
    print(f"Data directory: {data_dir}")
    print("Applying database migrations...")
    try:
        call_command("migrate", interactive=False, verbosity=0)
    except Exception as exc:
        sys.stderr.write(f"Error: failed to initialize database at '{db_path}': {exc}\n")
        return 1

    url = format_url(args.host, port)
    check_url = f"http://127.0.0.1:{port}/" if args.host == "0.0.0.0" else url

    print(f"\ncli2ui is running at: {url}")
    print("Press Ctrl+C to stop.\n")

    if not args.no_browser:
        open_browser_when_ready(check_url, url)

    try:
        call_command("runserver", f"{args.host}:{port}", use_reloader=False)
    except KeyboardInterrupt:
        print("\nShutting down cli2ui.")
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error: server terminated unexpectedly: {exc}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
