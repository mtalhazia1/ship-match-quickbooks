from django.urls import path

from . import views

app_name = "accounting"
urlpatterns = [
    path("qbo/connect/<int:org_id>/", views.connect, name="connect"),
    path("qbo/callback", views.callback, name="callback"),
    path("qbo/reconnect/", views.reconnect, name="reconnect"),
    # Settings > Accounting (QuickBooks and Xero); the path predates Xero and stays for saved links.
    path("qbo/settings/<int:org_id>/", views.qbo_settings, name="settings"),
    path("qbo/disconnect/<int:org_id>/", views.disconnect, name="disconnect"),
    path("xero/connect/<int:org_id>/", views.xero_connect, name="xero_connect"),
    path("xero/callback", views.xero_callback, name="xero_callback"),
    path("xero/organisation/<int:org_id>/", views.xero_tenant, name="xero_tenant"),
    path("xero/settings/<int:org_id>/", views.xero_settings, name="xero_settings"),
    path("xero/disconnect/<int:org_id>/", views.xero_disconnect, name="xero_disconnect"),
    path("payments/check/", views.check_payments, name="check_payments"),
]
