"""A tiny registry of optional feature panels.

An app registers its key in its AppConfig.ready(); the core nav shows a panel's
buttons only when its key is available (via the `enabled_features` context
processor), and the URLconf includes the panel's routes only when its app is
installed. Dropping the app from INSTALLED_APPS makes the feature vanish — nav
and routes alike — which keeps optional features cleanly separable.

The registry also carries edition metadata. A panel that belongs to an extended
edition registers with ``edition=EDITION_EXTENDED`` and reuses the same
availability checks instead of scattering edition decisions through templates
and views.
"""
from dataclasses import dataclass
from functools import wraps

from django.conf import settings
from django.http import Http404

EDITION_COMMUNITY = "community"
EDITION_EXTENDED = "extended"

_EDITION_RANK = {
    EDITION_COMMUNITY: 0,
    EDITION_EXTENDED: 1,
}


@dataclass(frozen=True)
class Feature:
    key: str
    edition: str = EDITION_COMMUNITY


_REGISTRY: dict[str, Feature] = {}


def current_edition() -> str:
    """The local edition this process exposes.

    Defaults to community. Unknown values deliberately fall back to community so
    a typo never unlocks an extended-edition feature by accident.
    """
    edition = getattr(settings, "CLI2UI_EDITION", EDITION_COMMUNITY)
    return edition if edition in _EDITION_RANK else EDITION_COMMUNITY


def register(key: str, *, edition: str = EDITION_COMMUNITY) -> None:
    """Mark a feature as present. Called from an app's AppConfig.ready()."""
    if edition not in _EDITION_RANK:
        raise ValueError(f"Unknown cli2ui edition: {edition}")
    _REGISTRY[key] = Feature(key=key, edition=edition)


def registered() -> dict[str, Feature]:
    """All registered feature metadata, regardless of current edition."""
    return dict(_REGISTRY)


def is_enabled(key: str) -> bool:
    feature = _REGISTRY.get(key)
    if feature is None:
        return False
    return _EDITION_RANK[current_edition()] >= _EDITION_RANK[feature.edition]


def enabled() -> set[str]:
    """The keys of all currently-available features."""
    return {key for key in _REGISTRY if is_enabled(key)}


def require(key: str):
    """Guard a view with a feature: 404 unless ``key`` is registered and allowed
    by the current edition. A view that is merely *installed* is not thereby
    reachable — its edition still decides."""
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not is_enabled(key):
                raise Http404
            return view(request, *args, **kwargs)
        return wrapper
    return decorator
