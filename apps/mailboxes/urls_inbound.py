from django.urls import path

from . import webhooks

app_name = "inbound"
urlpatterns = [
    path("postmark/", webhooks.postmark, name="postmark"),
    path("mailgun/", webhooks.mailgun, name="mailgun"),
]
