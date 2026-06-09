from django.urls import path

from core import views

urlpatterns = [
    path("", views.index, name="index"),
    path("connect", views.connect, name="connect"),
    path("c/<int:pk>/tables", views.tables, name="tables"),
]
