"""A tiny registry of optional feature panels.

An app registers its key in its AppConfig.ready(); the core nav shows a panel's
buttons only when its key is registered (via the `enabled_features` context
processor), and the URLconf includes the panel's routes only when its app is
installed. Dropping the app from INSTALLED_APPS makes the feature vanish — nav
and routes alike — which is what keeps future paid features cleanly separable.
"""
_REGISTRY: set[str] = set()


def register(key: str) -> None:
    """Mark a feature as present. Called from an app's AppConfig.ready()."""
    _REGISTRY.add(key)


def enabled() -> set[str]:
    """The keys of all currently-registered features."""
    return set(_REGISTRY)


def is_enabled(key: str) -> bool:
    return key in _REGISTRY
