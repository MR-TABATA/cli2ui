"""Encrypting the secrets cli2ui keeps (today: the password of a saved connection).

By default nothing is encrypted — cli2ui is a local, single-user tool and the
management database is yours alone. An app that knows how to encrypt installs a
*codec* (``register_codec``); from then on every ``SecretField`` is written
encrypted and read back decrypted, and the code that uses the value (the engines
connecting to a database) never notices.

Stored values carry a prefix, so a row written before a codec was installed (plain
text) is still read as it is, and is encrypted the next time it is saved. A
prefixed value that cannot be decrypted — no codec installed, or the key is gone —
is never handed out as if it were the secret: reading it raises SecretUnavailable.
Nothing here knows how the codec encrypts or where its keys live.
"""
from django.core.exceptions import ImproperlyConfigured
from django.db import models

PREFIX = "cli2ui-secret:v1:"


class SecretUnavailable(Exception):
    """A stored secret is encrypted and cannot be decrypted right now."""


_CODEC = None   # (encode, decode): str -> str, each


def register_codec(encode, decode) -> None:
    """Install the encryption used by every SecretField. One only: a second,
    different registration is an error, not a silent replacement — rows written
    with the first would become unreadable."""
    global _CODEC
    if _CODEC is not None and _CODEC != (encode, decode):
        raise ImproperlyConfigured("A secret codec is already registered.")
    _CODEC = (encode, decode)


def codec_registered() -> bool:
    return _CODEC is not None


def encrypt(plain: str) -> str:
    return plain if _CODEC is None else PREFIX + _CODEC[0](plain)


def decrypt(stored: str) -> str:
    if not stored.startswith(PREFIX):
        return stored                       # written before encryption was on: plain text
    if _CODEC is None:
        raise SecretUnavailable("This secret is stored encrypted, but no secret codec is installed.")
    try:
        return _CODEC[1](stored[len(PREFIX):])
    except Exception as exc:                # a wrong or retired key, or damaged data
        raise SecretUnavailable("This secret cannot be decrypted with the keys available.") from exc


class _SecretAttribute:
    """The model attribute behind a SecretField: it holds what the database has
    (ciphertext or plain text) and decrypts only when the value is asked for, so
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
    """A text column that is encrypted at rest once a codec is installed."""

    def contribute_to_class(self, cls, name, private_only=False):
        super().contribute_to_class(cls, name, private_only=private_only)
        setattr(cls, self.attname, _SecretAttribute(self.attname))

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value or value.startswith(PREFIX):
            return value                    # empty, or already encrypted
        return encrypt(value)
