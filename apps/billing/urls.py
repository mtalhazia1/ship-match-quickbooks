from django.urls import path

from . import views

app_name = "billing"
urlpatterns = [
    path("settings/billing/", views.billing_settings, name="settings"),
    path("settings/billing/checkout/", views.checkout, name="checkout"),
    path("settings/billing/portal/", views.portal, name="portal"),
    path("billing/stripe/webhook/", views.stripe_webhook, name="stripe_webhook"),
    path("dashboard/getting-started/hide/", views.dismiss_onboarding, name="dismiss_onboarding"),
]
