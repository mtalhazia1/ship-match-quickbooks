from django.urls import path

from . import views

app_name = "intake"
urlpatterns = [
    path("documents/<int:pk>/original/", views.original_file, name="original"),
    path("documents/<int:pk>/keep-whole/", views.keep_whole, name="keep_whole"),
]
