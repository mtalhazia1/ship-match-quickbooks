"""Plans (from settings.BILLING_PLANS) and what an organization's plan allows."""
from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings

FEATURE_LABELS = {
    "email_intake": "Email intake",
    "quickbooks": "QuickBooks posting",
    "checks": "Rate, duplicate and total checks",
    "exports": "CSV and Excel exports",
    "disputes": "Vendor disputes",
    "api": "API access",
    "webhooks": "Outgoing webhooks",
    "priority_support": "Priority support",
}


@dataclass
class Plan:
    key: str
    name: str
    price_id: str
    price: str
    documents: int
    users: int  # 0 = no limit
    features: list[str] = field(default_factory=list)

    @property
    def feature_labels(self) -> list[str]:
        return [FEATURE_LABELS.get(f, f) for f in self.features]

    @property
    def can_checkout(self) -> bool:
        return bool(self.price_id)

    @property
    def users_label(self) -> str:
        return "Unlimited users" if not self.users else f"Up to {self.users} users"


def all_plans() -> list[Plan]:
    out = []
    for key, p in (getattr(settings, "BILLING_PLANS", {}) or {}).items():
        out.append(Plan(key=key, name=p.get("name") or key.title(), price_id=p.get("price_id") or "",
                        price=str(p.get("price") or ""), documents=int(p.get("documents") or 0),
                        users=int(p.get("users") or 0), features=list(p.get("features") or [])))
    return out


def get_plan(key: str | None) -> Plan | None:
    return next((p for p in all_plans() if p.key == key), None)


def plan_for_price(price_id: str | None) -> Plan | None:
    if not price_id:
        return None
    return next((p for p in all_plans() if p.price_id and p.price_id == price_id), None)


@dataclass
class Limits:
    """What the organization may do this month. None = no limit (billing off, or billed outside ShipMatch)."""

    documents: int | None
    users: int | None
    features: set[str] | None   # None = every feature
    plan: Plan | None = None
    trial: bool = False

    def allows(self, feature: str) -> bool:
        return self.features is None or feature in self.features


UNLIMITED = Limits(documents=None, users=None, features=None)


def account_for(org):
    from .models import BillingAccount

    if org is None or org.pk is None:
        return None
    return BillingAccount.objects.filter(organization=org).first()


def limits_for(org, account=None) -> Limits:
    if not getattr(settings, "BILLING_ENABLED", False):
        return UNLIMITED
    account = account if account is not None else account_for(org)
    if account is None or not account.is_billed:
        return UNLIMITED
    plan = get_plan(account.plan) or get_plan(settings.BILLING_TRIAL_PLAN)
    trial_only = account.status == account.Status.TRIALING and not account.has_subscription
    documents = settings.BILLING_TRIAL_DOCUMENTS if trial_only else (plan.documents if plan else None)
    users = (plan.users or None) if plan else None
    features = set(plan.features) if plan else None
    return Limits(documents=documents or None, users=users, features=features, plan=plan, trial=trial_only)


def feature_allowed(org, feature: str) -> bool:
    return limits_for(org).allows(feature)
