from django.urls import path

from . import views

app_name = "disputes"
urlpatterns = [
    path("", views.dispute_list, name="list"),
    path("settings/", views.dispute_settings, name="settings"),
    path("new/<int:shipment_pk>/", views.create, name="create"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/edit/", views.edit, name="edit"),
    path("<int:pk>/send/", views.send, name="send"),
    path("<int:pk>/discard/", views.discard, name="discard"),
    path("<int:pk>/reply/", views.reply, name="reply"),
    path("<int:pk>/note/", views.note, name="note"),
    path("<int:pk>/follow-up/", views.follow_up, name="follow_up"),
    path("<int:pk>/credit/", views.credit, name="credit"),
    path("<int:pk>/resolve/", views.resolve, name="resolve"),
    path("<int:pk>/close/", views.close, name="close"),
    path("<int:pk>/release/", views.release, name="release"),
]
