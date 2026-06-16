"""Template context processors."""
from .features import enabled


def features(request):
    """Expose the set of registered optional features so nav templates can show
    a panel's buttons only when its app is installed:
    `{% if 'planner_lab' in enabled_features %}`."""
    return {"enabled_features": enabled()}
