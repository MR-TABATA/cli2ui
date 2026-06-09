from django.urls import path

from core import views

urlpatterns = [
    path("", views.index, name="index"),
    path("connect", views.connect, name="connect"),
    path("c/<int:pk>/", views.workspace, name="workspace"),
    path("c/<int:pk>/table", views.table_detail, name="table_detail"),
    path("c/<int:pk>/settings", views.settings, name="settings"),
    path("c/<int:pk>/settings/update", views.settings_update, name="settings_update"),
    path("c/<int:pk>/settings/reset", views.settings_reset, name="settings_reset"),
]
