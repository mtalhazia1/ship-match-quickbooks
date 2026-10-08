from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.shortcuts import redirect
from django.templatetags.static import static as static_url
from django.urls import include, path, reverse
from django.views.generic import RedirectView

from apps.api import api
from apps.core import views as core_views


def admin_login(request):
    """The admin uses the app's sign-in (lockout + two-factor), never its own form."""
    target = reverse("accounts:login")
    nxt = request.GET.get("next")
    return redirect(f"{target}?next={nxt}" if nxt else target)


admin.site.site_header = "ShipMatch platform admin"
admin.site.site_title = "ShipMatch admin"

urlpatterns = [
    path("", lambda r: redirect("core:dashboard")),
    path("favicon.ico", RedirectView.as_view(url=static_url("img/favicon.svg"), permanent=True)),
    path("health/", core_views.health, name="health"),
    path("health/ready/", core_views.ready, name="ready"),
    path("admin/login/", admin_login),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
    path("account/", include("apps.accounts.urls")),
    path("", include("apps.core.urls")),
    path("review/", include("apps.shipments.urls")),
    path("intake/", include("apps.intake.urls")),
    path("accounting/", include("apps.accounting.urls")),
    path("rates/", include("apps.rates.urls")),
    path("savings/", include("apps.rates.urls_savings")),
    path("roi/", include("apps.rates.urls_roi")),
    path("disputes/", include("apps.disputes.urls")),
    path("landed/", include("apps.landed.urls")),
    path("close/", include("apps.close.urls")),
    path("settings/alerts/", include("apps.notifications.urls")),
    path("settings/email/", include("apps.mailboxes.urls")),
    path("inbound/email/", include("apps.mailboxes.urls_inbound")),
    path("customs/", include("apps.customs.urls")),
    path("", include("apps.learning.urls")),
    path("", include("apps.workflow.urls")),
    path("", include("apps.demo.urls")),
    path("", include("apps.billing.urls")),
    path("signup/", include("apps.billing.urls_signup")),
    path("", include("apps.integrations.urls")),
]

handler403 = "apps.core.errors.permission_denied"
handler404 = "apps.core.errors.not_found"
handler500 = "apps.core.errors.server_error"

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
