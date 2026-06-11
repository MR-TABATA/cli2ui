from django.urls import path

from core import views

urlpatterns = [
    path("", views.index, name="index"),
    path("connect", views.connect, name="connect"),
    path("c/<int:pk>/", views.workspace, name="workspace"),
    path("c/<int:pk>/overview", views.overview, name="overview"),
    path("c/<int:pk>/table", views.table_detail, name="table_detail"),
    path("c/<int:pk>/query", views.query, name="query"),
    path("c/<int:pk>/query/run", views.query_run, name="query_run"),
    path("c/<int:pk>/explain", views.explain_run, name="explain_run"),
    path("c/<int:pk>/scale", views.scale_run, name="scale_run"),
    path("c/<int:pk>/snapshots", views.snapshots, name="snapshots"),
    path("c/<int:pk>/snapshots/save", views.snapshot_save, name="snapshot_save"),
    path("c/<int:pk>/snapshots/plan", views.snapshot_plan, name="snapshot_plan"),
    path("c/<int:pk>/snapshots/delete", views.snapshot_delete, name="snapshot_delete"),
    path("c/<int:pk>/snapshots/diff", views.snapshot_diff, name="snapshot_diff"),
    path("c/<int:pk>/activity", views.activity, name="activity"),
    path("c/<int:pk>/activity/cancel", views.activity_cancel, name="activity_cancel"),
    path("c/<int:pk>/activity/kill", views.activity_kill, name="activity_kill"),
    path("c/<int:pk>/objects", views.objects, name="objects"),
    path("c/<int:pk>/schemas/create", views.schema_create, name="schema_create"),
    path("c/<int:pk>/schemas/delete", views.schema_delete, name="schema_delete"),
    path("c/<int:pk>/roles/create", views.role_create, name="role_create"),
    path("c/<int:pk>/roles/delete", views.role_delete, name="role_delete"),
    path("c/<int:pk>/settings", views.settings, name="settings"),
    path("c/<int:pk>/settings/update", views.settings_update, name="settings_update"),
    path("c/<int:pk>/settings/reset", views.settings_reset, name="settings_reset"),
]
