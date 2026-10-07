"""Encrypting the secrets cli2ui keeps (today: the password of a saved connection).

A saved password is never stored in plain text. It is encrypted (Fernet, from the
`cryptography` package) when it is written and decrypted only when a connection is
opened, so the code that connects never notices.

Where the key comes from, in this order:

1. ``CLI2UI_SECRET_KEYS`` — one or more keys, comma separated, newest first. The first
   encrypts; every key decrypts, which is how a key is rotated (put a new one in front, run
   ``manage.py encrypt_secrets``, then drop the old one). Make a key with
   ``manage.py generate_secret_key``. This is the right place for a shared deployment: the key
   lives outside the database and its backups.
2. A key file — ``CLI2UI_SECRET_KEY_FILE``, by default ``secret.key`` next to the management
   database — created the first time it is needed, readable by its owner only. One key per
   line, newest first. It keeps a database file that leaks on its own from giving up the
   passwords; it does not help if the file leaks together with the database, so back them up
   separately.
3. For an in-memory database (tests, throwaway runs), a key that lives as long as the process.

A different scheme can replace all of this: an app may ``register_codec`` its own encryption
(a cloud key service, say). Then the key sources above are not used.

A stored value that cannot be decrypted — the key is gone, or it is not the one it was written
with — is never handed out as if it were the password: reading it raises SecretUnavailable,
while listing and unrelated updates keep working. A password found stored as plain text (put
there by hand, or by an older version) is refused the same way when it is *used*; saving the
connection, or ``manage.py encrypt_secrets``, writes it back encrypted.

A stored value can also be a *reference* — ``secret://<scheme>/<name>`` — instead of the
secret itself. It is looked up when the value is used, by a resolver an app registered for
that scheme (``register_resolver``), so the database never holds the password at all. A
reference holds no secret, so it is stored as typed. One that cannot be resolved (no resolver
for the scheme, or the resolver says no) raises SecretUnavailable, like an undecryptable value.
"""
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, models

PREFIX = "cli2ui-secret:v1:"
REF_PREFIX = "secret://"
KEYS_ENV = "CLI2UI_SECRET_KEYS"
DEFAULT_KEY_FILE_NAME = "secret.key"


class SecretUnavailable(Exception):
    """A stored secret cannot be decrypted or looked up right now."""


_CODEC = None           # an app's own (encode, decode), replacing the built-in Fernet one
_RESOLVERS: dict = {}   # scheme -> func(name) -> str
_BUILTIN = None         # (source signature, (encode, decode)), rebuilt when the key source changes
_EPHEMERAL: list = []   # the process-lifetime key for an in-memory database


class StoredSecret(str):
    """A value as the database holds it — ciphertext, a reference, or (put there by hand or an
    older version) plain text — as opposed to a plain password just typed in. Only the first
    two are ever used as stored; the third is refused."""


# --- keys ---------------------------------------------------------------------------

def generate_key() -> str:
    return Fernet.generate_key().decode()


def parse_keys(text: str, *, source: str, separator=",") -> list:
    keys = []
    for part in text.replace("\n", separator).split(separator):
        part = part.strip()
        if not part or part.startswith("#"):
            continue
        try:
            keys.append(Fernet(part.encode()))
        except (ValueError, TypeError) as exc:
            raise ImproperlyConfigured(
                f"{source} holds something that is not a Fernet key (32 url-safe base64 bytes). "
                "Make one with `manage.py generate_secret_key`.") from exc
    return keys


def _in_memory(name) -> bool:
    name = str(name or "")
    return name in ("", ":memory:") or name.startswith("file:memorydb") or "mode=memory" in name


def key_file_path():
    """The key file in use, or None for an in-memory database."""
    explicit = getattr(settings, "CLI2UI_SECRET_KEY_FILE", "") or os.environ.get("CLI2UI_SECRET_KEY_FILE", "")
    if explicit:
        return Path(explicit)
    name = connection.settings_dict.get("NAME")
    return None if _in_memory(name) else Path(str(name)).parent / DEFAULT_KEY_FILE_NAME


def _encrypted_rows_exist() -> bool:
    """Whether the database already holds a password encrypted with some key. If it does, a
    missing key file means the key was lost — not that this is a first run."""
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM core_connection WHERE password LIKE %s LIMIT 1", [PREFIX + "%"])
            return cur.fetchone() is not None
    except Exception:                       # the table does not exist yet (early migrations)
        return False


_FILE_KEYS: dict = {}   # (path, mtime) -> parsed keys, so a busy request does not re-read the file


def _read_or_create_key_file(path: Path) -> list:
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
        if stamp in _FILE_KEYS:
            return _FILE_KEYS[stamp]
    except OSError:
        stamp = None
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if _encrypted_rows_exist():
            # Making a new key now would quietly strand every password already saved.
            raise SecretUnavailable(f"The secret key file {path} is missing, but saved passwords are "
                                    "encrypted with the key that was in it. Restore the file (or set "
                                    "CLI2UI_SECRET_KEYS to the old key).")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # owner only, never overwritten
        except FileExistsError:
            text = path.read_text(encoding="utf-8")               # lost a race: use the winner's
        except OSError as exc:
            raise SecretUnavailable(f"Cannot create the secret key file {path}: {exc.strerror}. "
                                    "Make the folder writable, or set CLI2UI_SECRET_KEYS.") from exc
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("# cli2ui secret key (newest first). Back this up apart from the database.\n")
                handle.write(generate_key() + "\n")
            text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretUnavailable(f"Cannot read the secret key file {path}: {exc.strerror}.") from exc
    keys = parse_keys(text, source=str(path))
    if not keys:
        raise SecretUnavailable(f"The secret key file {path} holds no key.")
    _FILE_KEYS[(str(path), path.stat().st_mtime_ns)] = keys
    return keys


def _keys():
    """(keys, key source name, a signature that changes when the source does)."""
    raw = os.environ.get(KEYS_ENV, "").strip()
    if raw:
        return parse_keys(raw, source=KEYS_ENV), "env", ("env", raw)
    path = key_file_path()
    if path is None:
        if not _EPHEMERAL:
            _EPHEMERAL.append(Fernet(Fernet.generate_key()))
        return list(_EPHEMERAL), "memory", ("memory",)
    return _read_or_create_key_file(path), "file", ("file", str(path), path.stat().st_mtime_ns)


def key_source() -> str:
    """Where the key comes from right now: custom, env, file or memory."""
    return "custom" if _CODEC is not None else _keys()[1]


def _fernet_codec(keys):
    ring = MultiFernet(keys)

    def encode(plain: str) -> str:
        return ring.encrypt(plain.encode()).decode()

    def decode(token: str) -> str:
        try:
            return ring.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("no available key decrypts this") from exc

    return encode, decode


def _active_codec():
    global _BUILTIN
    if _CODEC is not None:
        return _CODEC
    keys, _, signature = _keys()
    if _BUILTIN is None or _BUILTIN[0] != signature:
        _BUILTIN = (signature, _fernet_codec(keys))
    return _BUILTIN[1]


def register_codec(encode, decode) -> None:
    """Replace the built-in encryption with an app's own. One only: a second, different
    registration is an error, not a silent replacement — rows written with the first would
    become unreadable."""
    global _CODEC
    if _CODEC is not None and _CODEC != (encode, decode):
        raise ImproperlyConfigured("A secret codec is already registered.")
    _CODEC = (encode, decode)


# --- references -----------------------------------------------------------------------

def register_resolver(scheme: str, resolve) -> None:
    """Install the lookup for ``secret://<scheme>/<name>`` references. ``resolve(name)``
    returns the secret or raises. One resolver per scheme; a second, different one is an error."""
    if not scheme or "/" in scheme or ":" in scheme:
        raise ValueError(f"Not a usable scheme: {scheme!r}")
    if scheme in _RESOLVERS and _RESOLVERS[scheme] is not resolve:
        raise ImproperlyConfigured(f"A resolver for {scheme!r} is already registered.")
    _RESOLVERS[scheme] = resolve


def is_reference(value: str) -> bool:
    return bool(value) and value.startswith(REF_PREFIX)


def resolve_reference(ref: str) -> str:
    scheme, _, name = ref[len(REF_PREFIX):].partition("/")
    resolver = _RESOLVERS.get(scheme)
    if resolver is None or not name:
        raise SecretUnavailable(f"No way to look up a {scheme or '(empty)'!r} secret reference here.")
    try:
        value = resolver(name)
    except Exception as exc:               # missing, not allowed, unreadable — say nothing more
        raise SecretUnavailable(f"The secret {scheme}/{name} could not be looked up.") from exc
    if value is None:
        raise SecretUnavailable(f"The secret {scheme}/{name} could not be looked up.")
    return value


# --- encrypt / decrypt ------------------------------------------------------------------

def encrypt(plain: str) -> str:
    return PREFIX + _active_codec()[0](plain)


def decrypt(stored: str) -> str:
    if is_reference(stored):
        return resolve_reference(stored)
    if not stored.startswith(PREFIX):
        if isinstance(stored, StoredSecret):
            raise SecretUnavailable("This password is stored as plain text, which cli2ui refuses to "
                                    "use. Encrypt what is stored with `manage.py encrypt_secrets`.")
        return stored                       # a password just typed in, not yet saved
    try:
        return _active_codec()[1](stored[len(PREFIX):])
    except SecretUnavailable:
        raise
    except Exception as exc:                # a wrong or retired key, or damaged data
        raise SecretUnavailable("This secret cannot be decrypted with the keys available.") from exc


class _SecretAttribute:
    """The model attribute behind a SecretField: it holds what the database has
    (ciphertext, a reference or plain text) and decrypts only when the value is asked for, so
    loading a list of connections never fails on one whose key is missing."""

    def __init__(self, name):
        self.name = name

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        stored = instance.__dict__.get(self.name)
        return decrypt(stored) if stored else stored

    def __set__(self, instance, value):
        instance.__dict__[self.name] = value


class SecretField(models.TextField):
    """A text column that is always encrypted at rest."""

    def contribute_to_class(self, cls, name, private_only=False):
        super().contribute_to_class(cls, name, private_only=private_only)
        setattr(cls, self.attname, _SecretAttribute(self.attname))

    def pre_save(self, model_instance, add):
        # Save what the attribute holds (ciphertext, a reference, or a password just typed in),
        # not what it decrypts to: asking for the decrypted value here would resolve a
        # reference and write the password it points at into the database.
        return model_instance.__dict__.get(self.attname)

    def from_db_value(self, value, expression, connection):
        return StoredSecret(value) if value else value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value or value.startswith(PREFIX) or is_reference(value):
            return value                    # empty, already encrypted, or a reference (holds no secret)
        return encrypt(str(value))          # typed in, or plain text found stored: written back encrypted
