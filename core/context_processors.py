"""Template context processors."""
from cli2ui import __version__

from .features import enabled


def features(request):
    """Expose the set of registered optional features so nav templates can show
    a panel's buttons only when its app is installed:
    `{% if 'planner_lab' in enabled_features %}`."""
    return {"enabled_features": enabled()}


def version(request):
    """Expose the project version (single source: cli2ui.__version__) so the
    footer can show which build is running, even without a DB connection."""
    return {"cli2ui_version": __version__}
