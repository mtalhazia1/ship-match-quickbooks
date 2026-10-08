from django.urls import path

from . import views

app_name = "integrations"
urlpatterns = [
    path("exports/<str:kind>/", views.export, name="export"),
    path("settings/webhooks/", views.webhooks, name="webhooks"),
    path("settings/webhooks/<int:pk>/", views.webhook_edit, name="webhook_edit"),
    path("settings/webhooks/<int:pk>/test/", views.webhook_test, name="webhook_test"),
    path("settings/webhooks/<int:pk>/rotate/", views.webhook_rotate, name="webhook_rotate"),
    path("settings/webhooks/<int:pk>/delete/", views.webhook_delete, name="webhook_delete"),
    path("settings/webhooks/deliveries/<int:pk>/replay/", views.delivery_replay, name="webhook_replay"),
]
