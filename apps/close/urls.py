from django.urls import path

from . import views

app_name = "close"
urlpatterns = [
    path("", views.index, name="index"),
    path("accruals/", views.accrual_report, name="accruals"),
    path("accruals/export.csv", views.accruals_csv, name="accruals_csv"),
    path("accruals/journal.xlsx", views.accruals_journal, name="accruals_journal"),
    path("accruals/lock/", views.lock_period, name="lock"),
    path("accruals/adjust/", views.adjust, name="adjust"),
    path("accruals/adjust/<int:pk>/remove/", views.remove_adjustment, name="remove_adjustment"),
    path("settings/", views.close_settings, name="settings"),
    path("statements/", views.statement_list, name="statements"),
    path("statements/upload/", views.statement_upload, name="statement_upload"),
    path("statements/<int:pk>/", views.statement_detail, name="statement"),
    path("statements/<int:pk>/file/", views.statement_file, name="statement_file"),
    path("statements/<int:pk>/export.csv", views.statement_export, name="statement_export"),
    path("statements/<int:pk>/edit/", views.statement_edit, name="statement_edit"),
    path("statements/<int:pk>/match/", views.statement_rematch, name="statement_rematch"),
    path("statements/<int:pk>/delete/", views.statement_delete, name="statement_delete"),
    path("findings/<int:pk>/resolve/", views.item_resolve, name="item_resolve"),
    path("payments/add/", views.payment_add, name="payment_add"),
    path("payments/<int:pk>/delete/", views.payment_delete, name="payment_delete"),
    path("payments/quickbooks/", views.payment_sync, name="payment_sync"),
]
