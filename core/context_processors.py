"""Template context processors."""
from cli2ui import __version__

from . import hosted
from .features import current_edition, enabled


def features(request):
    """Expose the set of registered optional features so nav templates can show
    a panel's buttons only when its app is installed:
    `{% if 'planner_lab' in enabled_features %}`."""
    return {
        "cli2ui_edition": current_edition(),
        "enabled_features": enabled(),
        "hosted_mode": hosted.is_hosted(),
        "hosted_disabled": hosted.disabled_capabilities(),
        "hosted_disabled_paths": hosted.disabled_paths(),
    }


def version(request):
    """Expose the project version (single source: cli2ui.__version__) so the
    footer can show which build is running, even without a DB connection."""
    return {"cli2ui_version": __version__}
