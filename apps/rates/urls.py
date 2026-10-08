from django.urls import path

from . import views

app_name = "rates"
urlpatterns = [
    path("", views.quote_list, name="list"),
    path("new/", views.quote_create, name="create"),
    path("export.csv", views.quote_export, name="export"),
    path("import/", views.import_quotes, name="import"),
    path("import/template.csv", views.import_template, name="template"),
    path("import/problems.csv", views.import_errors, name="import_errors"),
    path("<int:pk>/", views.quote_detail, name="detail"),
    path("<int:pk>/edit/", views.quote_edit, name="edit"),
    path("<int:pk>/archive/", views.quote_archive, name="archive"),
    path("<int:pk>/delete/", views.quote_delete, name="delete"),
    path("extras/", views.extras, name="extras"),
    path("extras/new/", views.extra_create, name="extra_create"),
    path("extras/<int:pk>/", views.extra_edit, name="extra_edit"),
    path("extras/<int:pk>/delete/", views.extra_delete, name="extra_delete"),
    path("charge-names/", views.charge_names, name="charge_names"),
    path("charge-names/<int:pk>/delete/", views.charge_name_delete, name="charge_name_delete"),
    path("rules/", views.rules, name="rules"),
    path("recheck/", views.recheck_all, name="recheck"),
]
