from django.urls import path

from apps.accounts import views_team

from . import views

app_name = "core"
urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("audit/", views.audit_log, name="audit"),
    path("reports/accuracy/", views.accuracy, name="accuracy"),
    path("audit/export.csv", views.audit_export, name="audit_export"),
    path("settings/", views.org_settings, name="settings"),
    path("settings/api-keys/", views.api_keys, name="api_keys"),
    path("settings/api-keys/<int:pk>/revoke/", views.revoke_api_key, name="revoke_api_key"),
    path("team/", views_team.team, name="team"),
    path("team/invite/", views_team.invite, name="invite"),
    path("team/<int:pk>/update/", views_team.update_member, name="update_member"),
    path("team/<int:pk>/remove/", views_team.remove_member, name="remove_member"),
    path("team/<int:pk>/reset-2fa/", views_team.reset_member_mfa, name="reset_member_mfa"),
    path("team/<int:pk>/password-link/", views_team.send_password_link, name="password_link"),
]
