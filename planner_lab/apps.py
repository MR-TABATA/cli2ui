from django.apps import AppConfig


class PlannerLabConfig(AppConfig):
    """The planner what-if tools (scale simulation + index lab) as a self-contained
    app, so the whole feature is one removable unit. Registering the feature key on
    load is what makes the core nav show its buttons; dropping this app from
    INSTALLED_APPS makes the feature vanish — nav and routes alike."""

    name = "planner_lab"

    def ready(self):
        from core.features import register
        register("planner_lab")
