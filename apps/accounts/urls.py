from django.contrib.auth import views as auth_views
from django.urls import path

from . import views, views_team

app_name = "accounts"
urlpatterns = [
    path("invitations/<str:token>/", views_team.accept_invite, name="accept_invite"),
    path("login/", views.login_view, name="login"),
    path("login/verify/", views.verify_view, name="verify"),
    path("logout/", views.logout_view, name="logout"),
    path("security/", views.security_view, name="security"),
    path("security/2fa/start/", views.mfa_start, name="mfa_start"),
    path("security/2fa/confirm/", views.mfa_confirm, name="mfa_confirm"),
    path("security/2fa/disable/", views.mfa_disable, name="mfa_disable"),
    path("security/2fa/recovery-codes/", views.mfa_recovery_codes, name="mfa_recovery_codes"),
    path("security/preferences/", views.preferences, name="preferences"),
    path("password/", views.PasswordChangeView.as_view(), name="password_change"),
    path("password/reset/", views.PasswordResetView.as_view(), name="password_reset"),
    path("password/reset/sent/", auth_views.PasswordResetDoneView.as_view(
        template_name="account/password_reset_done.html"), name="password_reset_done"),
    path("password/set/<uidb64>/<token>/", views.PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("password/set/done/", auth_views.PasswordResetCompleteView.as_view(
        template_name="account/password_set_done.html"), name="password_reset_complete"),
]
