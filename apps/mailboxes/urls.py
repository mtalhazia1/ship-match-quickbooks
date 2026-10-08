from django.urls import path

from . import views

app_name = "mailboxes"
urlpatterns = [
    path("", views.index, name="index"),
    path("forwarding-address/new/", views.regenerate_address, name="regenerate"),
    path("imap/new/", views.imap_new, name="imap_new"),
    path("microsoft/connect/", views.microsoft_connect, name="microsoft_connect"),
    path("microsoft/callback", views.microsoft_callback, name="microsoft_callback"),
    path("<int:pk>/", views.edit, name="edit"),
    path("<int:pk>/test/", views.test, name="test"),
    path("<int:pk>/check/", views.check, name="check"),
    path("<int:pk>/toggle/", views.toggle, name="toggle"),
    path("<int:pk>/remove/", views.remove, name="remove"),
    path("<int:pk>/folders/", views.refresh_folders, name="folders"),
]
