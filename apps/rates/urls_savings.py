from django.urls import path

from . import views_savings

app_name = "savings"
urlpatterns = [
    path("", views_savings.savings_summary, name="summary"),
    path("export.csv", views_savings.savings_export, name="export"),
]
