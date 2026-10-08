from django.urls import path

from . import views

app_name = "workflow"
urlpatterns = [
    # Review queue: bulk actions and their results
    path("work/bulk/", views.bulk_action, name="bulk"),
    path("work/batches/<str:batch_id>/", views.batch, name="batch"),
    # One shipment: assignment and the focused approval page
    path("work/shipments/<int:pk>/assign/", views.assign_shipment, name="assign"),
    path("work/shipments/<int:pk>/approval/", views.quick, name="quick"),
    path("work/shipments/<int:pk>/approve/", views.quick_approve, name="quick_approve"),
    path("work/shipments/<int:pk>/reject/", views.quick_reject, name="quick_reject"),
    path("approve/<str:token>/", views.approval_link, name="approval_link"),
    # Comments and mentions
    path("work/comments/new/", views.comment_create, name="comment_create"),
    path("work/comments/<int:pk>/edit/", views.comment_edit, name="comment_edit"),
    path("work/comments/<int:pk>/delete/", views.comment_delete, name="comment_delete"),
    path("work/members/", views.members, name="members"),
    # Notifications (the bell)
    path("notifications/", views.notification_list, name="notifications"),
    path("notifications/<int:pk>/open/", views.notification_open, name="notification_open"),
    path("notifications/read-all/", views.notifications_read_all, name="notifications_read_all"),
    # Keyboard shortcuts and personal preferences
    path("account/shortcuts/", views.shortcuts, name="shortcuts"),
    # Settings: assignment rules
    path("settings/assignment/", views.assignment_settings, name="assignment_settings"),
    path("settings/assignment/vendors/", views.vendor_rule_add, name="vendor_rule_add"),
    path("settings/assignment/vendors/<int:pk>/delete/", views.vendor_rule_delete, name="vendor_rule_delete"),
    path("settings/assignment/run/", views.assign_waiting, name="assign_waiting"),
    # Firm view across client organizations
    path("clients/", views.client_list, name="portfolio"),
    path("clients/new/", views.create_org, name="create_org"),
    path("my-work/", views.my_work, name="my_work"),
]
