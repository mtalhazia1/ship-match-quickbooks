from django.urls import path

from . import views_savings

app_name = "roi"
urlpatterns = [
    path("", views_savings.roi_calculator, name="calculator"),
]
