"""Dispute life cycle and how an open dispute affects its shipment.

Status flow:

    draft --send--> sent --vendor agrees--> acknowledged
    sent / acknowledged --record credit--> credit received --mark resolved--> resolved
    sent / acknowledged --record credit that settles it--> resolved
    sent / acknowledged --close--> closed without recovery
    draft --discard--> (deleted)

Rules for the shipment (see README, "Disputes"):

1. A draft has no effect on the shipment.
2. While a dispute waits for the vendor (sent or acknowledged), the shipment can't be approved and the
   disputed issues stay open.
3. An approver can approve without waiting: with a note, the disputed issues are resolved and the hold
   is lifted. The dispute stays open to collect the credit; the bill posts as invoiced.
4. Recording the credit, or closing the dispute with a reason, resolves the disputed issues that are
   still open, recorded under the person who did it (so maker-checker treats them as a preparer).
5. Approved and posted shipments are read only: a dispute raised after approval tracks the money
   but never changes the shipment.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from apps.accounting.models import vendor_key
from apps.core.utils import audit
from apps.documents.models import Document
from apps.shipments.models import Shipment, ValidationIssue
from apps.shipments.services.validation import update_status

from ..evidence import invoice_facts, is_disputable, item_snapshot, money
from ..models import Dispute, DisputeEvent, DisputeItem, DisputeSettings, VendorContact
from . import drafting

NOTE_MIN = 10
AMOUNT_MAX = Decimal("999999999999.99")


class DisputeError(ValueError):
    """A request that can't be done; the message says why and what to do."""


# --------------------------------------------------------------------------- helpers


def _audit(dispute: Dispute, action: str, user, **extra):
    return audit(dispute.organization, action, dispute, actor=user, reference=dispute.reference,
                 vendor=dispute.vendor_name, amount=money(dispute.amount_disputed, dispute.currency),
                 shipment=dispute.shipment_reference, **extra)


def _event(dispute: Dispute, kind: str, user=None, text: str = "", **data) -> DisputeEvent:
    return DisputeEvent.objects.create(dispute=dispute, kind=kind, actor=user if getattr(user, "is_authenticated", False)
                                       else None, text=text[:4000], data=data)


def _touch(dispute: Dispute, user, *fields: str) -> None:
    dispute.updated_by = user if getattr(user, "is_authenticated", False) else None
    dispute.save(update_fields=[*fields, "updated_by", "updated_at"])


def parse_amount(raw, label: str = "Amount") -> Decimal:
    try:
        value = Decimal(str(raw).replace(",", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise DisputeError(f"{label} should be a number, for example 1250.00.")
    if value < 0 or value > AMOUNT_MAX:
        raise DisputeError(f"{label} can't be negative or that large.")
    return value


def clean_emails(raw: str, label: str, required: bool = False) -> list[str]:
    emails = [a.strip() for a in (raw or "").replace(";", ",").split(",") if a.strip()]
    if required and not emails:
        raise DisputeError(f"Add the {label}.")
    for e in emails:
        try:
            validate_email(e)
        except ValidationError:
            raise DisputeError(f"“{e}” is not a valid email address. Check the {label}.")
    return emails


def remember_contact(dispute: Dispute) -> None:
    if not dispute.vendor_email or not dispute.vendor_key:
        return
    VendorContact.objects.update_or_create(
        organization=dispute.organization, vendor_key=dispute.vendor_key,
        defaults={"email": dispute.vendor_email, "name": dispute.contact_name[:200],
                  "vendor_name": dispute.vendor_name[:200]})


def follow_up_default(org) -> date:
    days = DisputeSettings.for_org(org).follow_up_days or 7
    return timezone.localdate() + timezone.timedelta(days=days)


def _disputable_issues(shipment: Shipment, invoice: Document, issue_ids) -> list[ValidationIssue]:
    ids = {int(i) for i in issue_ids if str(i).isdigit()}
    issues = list(ValidationIssue.objects.filter(pk__in=ids, shipment=shipment, document=invoice)
                  .select_related("document"))
    if not issues or len(issues) != len(ids):
        raise DisputeError("Choose at least one issue on this invoice to raise with the vendor.")
    if not all(is_disputable(i) for i in issues):
        raise DisputeError("Some of the chosen checks are about our own paperwork, not the invoice. "
                           "Choose only problems with what the vendor billed.")
    return issues


def _set_items(dispute: Dispute, issues: list[ValidationIssue]) -> None:
    inv = invoice_facts(dispute.invoice, dispute.shipment)
    dispute.items.all().delete()
    for issue in issues:
        DisputeItem.objects.create(dispute=dispute, **item_snapshot(issue, inv))


def _invoice_sender(invoice) -> str:
    """The address the invoice was emailed from, as a starting point for "To" when no contact is saved yet."""
    import re

    email = getattr(invoice, "email", None)
    m = re.search(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", getattr(email, "sender", "") or "")
    return m.group(0) if m else ""


def _invoice_total(dispute: Dispute) -> Decimal | None:
    """The disputed invoice's printed total, when it can be read."""
    from apps.documents.services.locate import to_decimal

    if dispute.invoice is None:
        return None
    total = to_decimal(dispute.invoice.field("total_amount"))
    return total if total is not None and total > 0 else None


def default_amount(dispute: Dispute) -> Decimal:
    """Sum of the linked issues' money at risk in the dispute's currency."""
    total = Decimal("0.00")
    for item in dispute.items.all():
        if item.amount and (not item.currency or item.currency == dispute.currency):
            total += item.amount
    return total


# --------------------------------------------------------------------------- drafting


@transaction.atomic
def create_draft(shipment: Shipment, invoice: Document, issue_ids, user, use_ai: bool = True) -> tuple[Dispute, str]:
    """New draft for one invoice. Returns the dispute and a note about the wording (empty if none)."""
    org = shipment.organization
    if invoice.organization_id != org.pk or getattr(getattr(invoice, "match", None), "shipment_id", None) != shipment.pk:
        raise DisputeError("That invoice is not part of this shipment.")
    if not invoice.is_payable:
        raise DisputeError("Only invoices can be disputed.")
    issues = _disputable_issues(shipment, invoice, issue_ids)
    existing = Dispute.objects.filter(invoice=invoice, status__in=Dispute.OPEN).first()
    if existing:
        raise DisputeError(f"{existing.reference} is already open for this invoice. Add to it instead of starting another.")

    data = invoice.data()
    name = str(data.get("vendor_name") or "").strip()
    if not name:
        raise DisputeError("The invoice has no vendor name. Add it in the review screen first.")
    key = vendor_key(name)
    contact = VendorContact.objects.filter(organization=org, vendor_key=key).first()
    dispute = Dispute.objects.create(
        organization=org, shipment=shipment, shipment_reference=shipment.reference, invoice=invoice,
        invoice_number=str(data.get("invoice_number") or "")[:60], vendor_name=name[:200], vendor_key=key[:200],
        contact_name=contact.name if contact else "",
        vendor_email=contact.email if contact and contact.email else _invoice_sender(invoice),
        currency=(data.get("currency") or org.home_currency or "").upper()[:3], created_by=user, updated_by=user)
    _set_items(dispute, issues)
    dispute.amount_disputed = default_amount(dispute)
    dispute.subject, dispute.body = drafting.compose(dispute)
    note = ""
    if use_ai:
        note = _try_polish(dispute)
    dispute.save()
    _event(dispute, DisputeEvent.Kind.CREATED, user, f"{len(issues)} issue{'s' if len(issues) != 1 else ''} "
           f"on invoice {dispute.invoice_number or invoice.original_filename}",
           issues=[i.code for i in issues], ai=dispute.ai_polished)
    _audit(dispute, "dispute.drafted", user, issues=[i.code for i in issues], ai=dispute.ai_polished)
    return dispute, note


def _try_polish(dispute: Dispute) -> str:
    from apps.documents.services import llm

    if not llm.is_enabled():
        return ""
    try:
        dispute.subject, dispute.body = drafting.polish(dispute, dispute.subject, dispute.body)
        dispute.ai_polished = True
        return "AI reworded the email. Check it before sending."
    except drafting.PolishRejected as e:
        return f"The standard wording was kept: {e}"
    except Exception as e:  # LLMError, network, bad JSON: the template is always good enough
        drafting.log.warning("Dispute wording by AI failed: %s", e)
        return "AI wording was not available, so the standard wording was kept."


def repolish(dispute: Dispute, user) -> str:
    if dispute.status != Dispute.Status.DRAFT:
        raise DisputeError("Only drafts can be reworded.")
    note = _try_polish(dispute)
    if dispute.ai_polished:
        _touch(dispute, user, "subject", "body", "ai_polished")
    return note or "AI wording is not switched on for this server."


def rebuild(dispute: Dispute, user) -> None:
    """Throw away edits and write the email again from the evidence."""
    if dispute.status != Dispute.Status.DRAFT:
        raise DisputeError("Only drafts can be rebuilt.")
    dispute.subject, dispute.body = drafting.compose(dispute)
    dispute.ai_polished = False
    _touch(dispute, user, "subject", "body", "ai_polished")


@transaction.atomic
def update_draft(dispute: Dispute, user, *, vendor_email: str, contact_name: str, cc: str, subject: str, body: str,
                 amount: str, issue_ids, follow_up: str, remember: bool) -> list[str]:
    """Save the composer form. Returns notes for the user (for example, when the text was refreshed)."""
    if dispute.status != Dispute.Status.DRAFT:
        raise DisputeError(f"{dispute.reference} was already sent, so the email can't be changed.")
    notes: list[str] = []
    email = clean_emails(vendor_email, "vendor's email address")
    if len(email) > 1:
        raise DisputeError("Put one address in the To field and the others in Copy to.")
    clean_emails(cc, "copy addresses")
    subject = " ".join((subject or "").split())[:drafting.SUBJECT_MAX]
    body = (body or "").replace("\r\n", "\n")
    if len(body) > drafting.BODY_MAX:
        raise DisputeError(f"The email is too long. Keep it under {drafting.BODY_MAX:,} characters.")
    follow = None
    if (follow_up or "").strip():
        try:
            follow = date.fromisoformat(follow_up.strip())
        except ValueError:
            raise DisputeError("Follow-up date should be a date such as 2026-10-15.")
        if follow < timezone.localdate():
            raise DisputeError("The follow-up date is in the past. Choose today or a later date.")

    old_ids = set(dispute.items.exclude(issue=None).values_list("issue_id", flat=True))
    new_ids = {int(i) for i in issue_ids if str(i).isdigit()}
    old_amount, old_contact = dispute.amount_disputed, dispute.contact_name
    old_subject, old_body = dispute.subject, dispute.body
    issues_changed = new_ids != old_ids
    if issues_changed:
        if dispute.shipment is None or dispute.invoice is None:
            raise DisputeError("The shipment or invoice of this dispute was removed, so issues can't be changed.")
        _set_items(dispute, _disputable_issues(dispute.shipment, dispute.invoice, new_ids))

    entered = parse_amount(amount, "Amount disputed") if str(amount).strip() else None
    if entered is not None:
        invoice_total = _invoice_total(dispute)
        if invoice_total is not None and entered > invoice_total:
            raise DisputeError(f"The amount disputed ({money(entered, dispute.currency)}) is more than the whole "
                               f"invoice ({money(invoice_total, dispute.currency)}).")
        if entered == 0 and default_amount(dispute) > 0:
            raise DisputeError(f"The issues put {money(default_amount(dispute), dispute.currency)} at risk, so the "
                               "amount disputed can't be 0. Leave the amount empty to use that figure.")
    if entered is None or (issues_changed and entered == old_amount):
        dispute.amount_disputed = default_amount(dispute)  # follow the issues unless the person typed an amount
    else:
        dispute.amount_disputed = entered

    dispute.vendor_email = email[0] if email else ""
    dispute.contact_name = (contact_name or "").strip()[:200]
    dispute.cc = ", ".join(clean_emails(cc, "copy addresses"))[:500]
    dispute.follow_up_on = follow
    facts_changed = issues_changed or dispute.amount_disputed != old_amount
    text_untouched = (subject == " ".join(old_subject.split())
                      and body.replace("\r\n", "\n").strip() == old_body.replace("\r\n", "\n").strip())
    if text_untouched and (facts_changed or dispute.contact_name != old_contact):
        dispute.subject, dispute.body = drafting.compose(dispute)
        dispute.ai_polished = False
        if facts_changed:
            notes.append("The email was written again for the new issues and amount.")
    else:
        dispute.subject, dispute.body = subject, body
        if facts_changed:
            notes.append("The issues or amount changed but your edited text was kept. "
                         "Use “Write it again” to refresh the email from the evidence.")
    dispute.updated_by = user
    dispute.save()
    if remember:
        remember_contact(dispute)
    _audit(dispute, "dispute.updated", user)
    return notes


def discard(dispute: Dispute, user) -> None:
    if dispute.status != Dispute.Status.DRAFT:
        raise DisputeError("Only drafts can be discarded. Close a sent dispute instead.")
    _audit(dispute, "dispute.discarded", user)
    dispute.delete()


# --------------------------------------------------------------------------- after sending


def log_reply(dispute: Dispute, user, text: str, agreed: bool, follow_up: str = "") -> None:
    if dispute.status == Dispute.Status.DRAFT:
        raise DisputeError("Send the dispute before logging a reply.")
    text = (text or "").strip()
    if not text:
        raise DisputeError("Paste or summarize what the vendor said.")
    fields = []
    if agreed and dispute.status == Dispute.Status.SENT:
        dispute.status = Dispute.Status.ACKNOWLEDGED
        fields.append("status")
    if (follow_up or "").strip() and dispute.is_waiting:
        _set_follow_up_value(dispute, follow_up)
        fields.append("follow_up_on")
    if fields:
        _touch(dispute, user, *fields)
    _event(dispute, DisputeEvent.Kind.REPLY, user, text, agreed=agreed)
    _audit(dispute, "dispute.reply_logged", user, agreed=agreed)


def add_note(dispute: Dispute, user, text: str) -> None:
    text = (text or "").strip()
    if not text:
        raise DisputeError("Write the note first.")
    _event(dispute, DisputeEvent.Kind.NOTE, user, text)
    _audit(dispute, "dispute.note_added", user)


def _set_follow_up_value(dispute: Dispute, raw: str) -> None:
    try:
        when = date.fromisoformat(str(raw).strip())
    except ValueError:
        raise DisputeError("Follow-up date should be a date such as 2026-10-15.")
    if when < timezone.localdate():
        raise DisputeError("The follow-up date is in the past. Choose today or a later date.")
    dispute.follow_up_on = when


def set_follow_up(dispute: Dispute, user, raw: str) -> None:
    if not dispute.is_waiting:
        raise DisputeError("Follow-up dates apply only while waiting for the vendor.")
    _set_follow_up_value(dispute, raw)
    _touch(dispute, user, "follow_up_on")
    _event(dispute, DisputeEvent.Kind.FOLLOW_UP, user, f"Follow up on {dispute.follow_up_on:%d %b %Y}",
           date=dispute.follow_up_on.isoformat())
    _audit(dispute, "dispute.follow_up_changed", user, date=dispute.follow_up_on.isoformat())


@transaction.atomic
def record_credit(dispute: Dispute, user, amount: str, credit_note_id: str = "", settles: bool = False,
                  note: str = "", confirm_over: bool = False) -> None:
    """The vendor issued a credit note or a corrected invoice. `amount` is the total recovered so far."""
    if dispute.status not in Dispute.WAITING | {Dispute.Status.CREDIT_RECEIVED}:
        raise DisputeError("A credit can be recorded only on a dispute that was sent and is still open.")
    value = parse_amount(amount, "Amount recovered")
    if value <= 0:
        raise DisputeError("Enter the amount the vendor credited. To end the dispute with nothing back, close it instead.")
    invoice_total = _invoice_total(dispute)
    if invoice_total is not None and value > invoice_total:
        raise DisputeError(f"A credit of {money(value, dispute.currency)} is more than the whole invoice "
                           f"({money(invoice_total, dispute.currency)}). Check the amount.")
    if value > dispute.amount_disputed > 0 and not confirm_over:
        raise DisputeError(f"The credit ({money(value, dispute.currency)}) is more than the amount disputed "
                           f"({money(dispute.amount_disputed, dispute.currency)}). If that is right, tick “The credit is "
                           "higher than the amount disputed” and save again.")
    credit_doc = None
    if str(credit_note_id or "").strip():
        credit_doc = Document.objects.filter(organization=dispute.organization, pk=str(credit_note_id).strip()
                                             if str(credit_note_id).strip().isdigit() else 0).first()
        if credit_doc is None:
            raise DisputeError("Choose the credit note from this organization's documents.")
        if credit_doc.pk == dispute.invoice_id:
            raise DisputeError("That is the disputed invoice itself. Choose the credit note or corrected invoice.")
    org = dispute.organization
    dispute.amount_recovered = value
    dispute.amount_recovered_home = org.to_home(value, dispute.currency)
    dispute.credit_note = credit_doc or dispute.credit_note
    dispute.recovered_at = timezone.now()
    dispute.status = Dispute.Status.CREDIT_RECEIVED
    note = (note or "").strip()[:500]
    _touch(dispute, user, "amount_recovered", "amount_recovered_home", "credit_note", "recovered_at", "status")
    text = f"{money(value, dispute.currency)} recovered" + (f" with {credit_doc.original_filename}" if credit_doc else "")
    _event(dispute, DisputeEvent.Kind.CREDIT, user, text + (f". {note}" if note else ""),
           amount=str(value), document=credit_doc.pk if credit_doc else None)
    _audit(dispute, "dispute.credit_received", user, recovered=money(value, dispute.currency),
           credit_note=credit_doc.original_filename if credit_doc else "")
    resolve_linked_issues(dispute, user, f"{dispute.reference}: vendor credited {money(value, dispute.currency)}."
                          + (f" {note}" if note else ""))
    if settles:
        resolve(dispute, user, note)


def resolve(dispute: Dispute, user, note: str = "") -> None:
    if dispute.status != Dispute.Status.CREDIT_RECEIVED:
        raise DisputeError("Record the credit first. To end the dispute with nothing back, close it instead.")
    dispute.status, dispute.closed_at = Dispute.Status.RESOLVED, timezone.now()
    dispute.outcome_note = (note or "").strip()[:500]
    _touch(dispute, user, "status", "closed_at", "outcome_note")
    short = dispute.outstanding
    text = (f"Recovered {money(dispute.amount_recovered, dispute.currency)} of "
            f"{money(dispute.amount_disputed, dispute.currency)}")
    if short > 0:
        text += f"; {money(short, dispute.currency)} written off"
    _event(dispute, DisputeEvent.Kind.RESOLVED, user, text + (f". {dispute.outcome_note}" if dispute.outcome_note else ""))
    _audit(dispute, "dispute.resolved", user, recovered=money(dispute.amount_recovered, dispute.currency))


@transaction.atomic
def close(dispute: Dispute, user, reason: str) -> None:
    if dispute.status not in Dispute.WAITING:
        if dispute.status == Dispute.Status.DRAFT:
            raise DisputeError("This dispute was never sent. Discard the draft instead.")
        if dispute.status == Dispute.Status.CREDIT_RECEIVED:
            raise DisputeError("A credit was already recorded. Mark the dispute resolved instead.")
        raise DisputeError(f"{dispute.reference} is already {dispute.get_status_display().lower()}.")
    reason = (reason or "").strip()[:500]
    if len(reason) < NOTE_MIN:
        raise DisputeError(f"Say why the dispute ends without recovery (at least {NOTE_MIN} characters). "
                           "It is kept in the audit log.")
    dispute.status, dispute.closed_at, dispute.outcome_note = Dispute.Status.CLOSED, timezone.now(), reason
    _touch(dispute, user, "status", "closed_at", "outcome_note")
    _event(dispute, DisputeEvent.Kind.CLOSED, user, reason)
    _audit(dispute, "dispute.closed", user, note=reason)
    resolve_linked_issues(dispute, user, f"{dispute.reference} closed without recovery: {reason}")


@transaction.atomic
def release_hold(dispute: Dispute, user, note: str) -> int:
    """Approve without waiting: resolve the disputed issues so the shipment can be approved."""
    if not dispute.holds_shipment:
        raise DisputeError(f"{dispute.reference} is not holding the shipment.")
    if dispute.shipment and dispute.shipment.is_locked:
        raise DisputeError("The shipment is already approved.")
    note = (note or "").strip()[:500]
    if len(note) < NOTE_MIN:
        raise DisputeError(f"Explain why the shipment can be approved before the vendor answers "
                           f"(at least {NOTE_MIN} characters). It is kept in the audit log.")
    dispute.hold_released, dispute.hold_released_by, dispute.hold_released_at = True, user, timezone.now()
    dispute.hold_release_note = note
    _touch(dispute, user, "hold_released", "hold_released_by", "hold_released_at", "hold_release_note")
    _event(dispute, DisputeEvent.Kind.RELEASED, user, note)
    _audit(dispute, "dispute.hold_released", user, note=note)
    return resolve_linked_issues(dispute, user, f"Approved without waiting for {dispute.reference}: {note}")


def resolve_linked_issues(dispute: Dispute, user, note: str) -> int:
    """Resolve the dispute's issues that are still open, unless the shipment is read only."""
    shipment = dispute.shipment
    if shipment is None or shipment.is_locked:
        return 0
    n = 0
    for item in dispute.items.select_related("issue"):
        issue = item.issue
        if issue is None or issue.resolved:
            continue
        issue.resolved, issue.resolved_by, issue.resolved_at = True, user, timezone.now()
        issue.resolution_note = note[:500]
        issue.save(update_fields=["resolved", "resolved_by", "resolved_at", "resolution_note"])
        audit(dispute.organization, "issue.resolved", issue, actor=user, code=issue.code, severity=issue.severity,
              note=note[:500], dispute=dispute.reference)
        n += 1
    if n:
        shipment.refresh_from_db()
        update_status(shipment)
    return n


# --------------------------------------------------------------------------- hooks


def approval_blockers(shipment: Shipment, user) -> list[str]:
    """Registered with the approval rules: a dispute waiting for the vendor holds the shipment."""
    reasons = []
    for d in Dispute.objects.filter(shipment=shipment, status__in=Dispute.WAITING, hold_released=False):
        reasons.append(f"{d.reference} with {d.vendor_name} is waiting for the vendor "
                       f"({money(d.amount_disputed, d.currency)} disputed). Record the credit note or corrected "
                       "invoice on the dispute, or an approver can approve without waiting.")
    return reasons


def relink_issue(issue: ValidationIssue) -> None:
    """Checking a shipment again re-creates its open issues; reattach them to their open disputes."""
    if not issue.shipment_id:
        return
    DisputeItem.objects.filter(issue__isnull=True, fingerprint=issue.fingerprint,
                               dispute__shipment_id=issue.shipment_id,
                               dispute__status__in=Dispute.OPEN).update(issue=issue)
