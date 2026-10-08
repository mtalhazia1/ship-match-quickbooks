from django.urls import path

from . import views

app_name = "learning"
urlpatterns = [
    path("settings/learning/", views.learning_settings, name="settings"),
    path("settings/learning/<int:pk>/forget/", views.forget, name="forget"),
]
