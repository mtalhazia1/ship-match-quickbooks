from django.urls import path

from . import views

app_name = "review"
urlpatterns = [
    path("", views.queue, name="queue"),
    path("documents/", views.documents, name="documents"),
    path("search/", views.search, name="search"),
    path("upload/", views.upload, name="upload"),
    path("shipments/<int:pk>/", views.shipment_detail, name="shipment"),
    path("shipments/<int:pk>/approve/", views.approve, name="approve"),
    path("shipments/<int:pk>/reject/", views.reject, name="reject"),
    path("shipments/<int:pk>/reopen/", views.reopen, name="reopen"),
    path("shipments/<int:pk>/post/", views.post_to_qbo, name="post"),
    path("documents/<int:pk>/", views.document_detail, name="document"),
    path("documents/<int:pk>/file/", views.document_file, name="document_file"),
    path("documents/<int:pk>/field/", views.update_field, name="update_field"),
    path("documents/<int:pk>/account/", views.set_account, name="set_account"),
    path("documents/<int:pk>/type/", views.set_type, name="set_type"),
    path("documents/<int:pk>/move/", views.move_document, name="move_document"),
    path("issues/<int:pk>/resolve/", views.resolve_issue, name="resolve_issue"),
]
