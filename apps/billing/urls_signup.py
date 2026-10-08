from django.urls import path

from . import views_signup

app_name = "signup"
urlpatterns = [
    path("", views_signup.signup_page, name="start"),
    path("check-your-email/", views_signup.sent, name="sent"),
    path("verify/<str:token>/", views_signup.verify, name="verify"),
    path("resend/", views_signup.resend, name="resend"),
]
