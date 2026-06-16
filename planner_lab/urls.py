"""Routes for the planner what-if panels. Included at the project root only when
this app is installed, so removing the app removes these URLs too. Names are kept
un-namespaced and identical to the originals, so existing {% url %} references in
the core nav resolve unchanged when the app is present."""
from django.urls import path

from . import views

urlpatterns = [
    path("c/<int:pk>/scale", views.scale_run, name="scale_run"),
    path("c/<int:pk>/lab", views.index_lab, name="index_lab"),
    path("c/<int:pk>/lab/preview", views.index_lab_preview, name="index_lab_preview"),
]
