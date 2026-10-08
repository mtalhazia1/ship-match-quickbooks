from django.urls import path

from . import views

app_name = "demo"
urlpatterns = [
    path("try/", views.try_page, name="try"),
    path("try/r/<str:token>/", views.try_result, name="try_result"),
    path("try/r/<str:token>/delete/", views.try_delete, name="try_delete"),
]
