"""Settings > Vendor learning: what ShipMatch learned from reviewer corrections, per vendor."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.permissions import require
from apps.core.utils import audit, current_org

from .models import VendorProfile
from .templatetags.learning_tags import field_label


@login_required
def learning_settings(request):
    org = current_org(request)
    require(request.user, org, "manage")
    profiles = list(VendorProfile.objects.filter(organization=org)
                    .annotate(helped=Count("documents", filter=~Q(documents__fields={}), distinct=True))
                    .order_by("display_name", "doc_type"))
    for p in profiles:
        p.facts = [f"{field_label(name)} is printed after “{info.get('label', '')}”" for name, info in sorted(p.labels.items())]
        if p.date_format:
            p.facts.append("Dates are written day first" if p.date_format == "dmy" else "Dates are written month first")
        p.recent = [{"field": field_label(e.get("field", "")), "old": e.get("old") or "", "new": e.get("new") or ""}
                    for e in reversed(p.examples[-3:])]
    return render(request, "learning/settings.html", {"profiles": profiles})


@login_required
@require_POST
def forget(request, pk):
    org = current_org(request)
    require(request.user, org, "manage")
    profile = get_object_or_404(VendorProfile, pk=pk, organization=org)
    name, doc_type = profile.display_name, profile.get_doc_type_display()
    audit(org, "learning.forgotten", profile, actor=request.user, vendor=name, doc_type=profile.doc_type,
          corrections=profile.correction_count, labels={k: v.get("label") for k, v in profile.labels.items()},
          date_format=profile.date_format)
    profile.delete()
    messages.success(request, f"ShipMatch forgot what it learned about {name} ({doc_type.lower()}s). "
                              "Their next documents are read as if they were new.")
    return redirect("learning:settings")
