from django.urls import path

from . import views

app_name = "notifications"
urlpatterns = [
    path("", views.alert_settings, name="settings"),
    path("channels/new/", views.channel_form, name="channel_new"),
    path("channels/<int:pk>/", views.channel_form, name="channel_edit"),
    path("channels/<int:pk>/test/", views.test_channel, name="channel_test"),
    path("channels/<int:pk>/delete/", views.delete_channel, name="channel_delete"),
]
