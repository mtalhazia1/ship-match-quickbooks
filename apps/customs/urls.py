from django.urls import path

from . import views

app_name = "customs"
urlpatterns = [
    path("free-time/", views.free_time, name="free_time"),
    path("free-time/<int:pk>/dates/", views.set_dates, name="set_dates"),
    path("settings/", views.customs_settings, name="settings"),
]
