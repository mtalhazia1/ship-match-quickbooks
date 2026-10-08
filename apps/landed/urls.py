from django.urls import path

from . import views

app_name = "landed"
urlpatterns = [
    path("", views.report, name="report"),
    path("export.<str:fmt>", views.report_export, name="report_export"),
    path("settings/", views.settings_view, name="settings"),
    path("shipments/<int:pk>/method/", views.shipment_method, name="shipment_method"),
    path("shipments/<int:pk>/export.<str:fmt>", views.shipment_export, name="shipment_export"),
    path("shipments/<int:pk>/share/", views.split_start, name="split_start"),
    path("invoices/<int:pk>/split/", views.split_save, name="split_save"),
    path("invoices/<int:pk>/confirm/", views.split_confirm, name="split_confirm"),
    path("invoices/<int:pk>/not-shared/", views.split_dismiss, name="split_dismiss"),
    path("invoices/<int:pk>/reset/", views.split_reset, name="split_reset"),
]
