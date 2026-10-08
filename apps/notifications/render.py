"""Turn a Message into what each service expects.

Slack incoming webhooks take Block Kit JSON: https://api.slack.com/messaging/webhooks and
https://api.slack.com/reference/block-kit/blocks (header text max 150 chars, up to 10 section fields).
Teams workflow webhooks ("When a Teams webhook request is received" in Power Automate) take a message
with an Adaptive Card attachment: https://learn.microsoft.com/microsoftteams/platform/task-modules-and-cards/cards/cards-reference
"""
from __future__ import annotations

from django.template.loader import render_to_string

from .events import Message

TEAMS_COLORS = {"error": "Attention", "warning": "Warning", "good": "Good", "info": "Default"}
EMAIL_COLORS = {"error": "#b42318", "warning": "#9a5a06", "good": "#0b7a43", "info": "#0e6b67"}


def _slack_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut(text: str, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def footer(msg: Message) -> str:
    return f"Sent by ShipMatch for {msg.org_name}" if msg.org_name else "Sent by ShipMatch"


def slack_payload(msg: Message) -> dict:
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": _cut(msg.title, 150), "emoji": False}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _cut(_slack_escape(msg.text), 3000)}},
    ]
    if msg.facts:
        blocks.append({"type": "section", "fields": [
            {"type": "mrkdwn", "text": _cut(f"*{_slack_escape(k)}*\n{_slack_escape(v)}", 2000)} for k, v in msg.facts[:10]
        ]})
    if msg.url:
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "text": {"type": "plain_text", "text": _cut(msg.link_label, 75), "emoji": False},
            "url": msg.url[:3000], "action_id": "open_shipmatch",
        }]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _slack_escape(footer(msg))}]})
    # `text` is the notification preview and the fallback for clients that can't show blocks.
    return {"text": _cut(f"{msg.title}: {msg.text}", 3000), "blocks": blocks}


def teams_payload(msg: Message) -> dict:
    body: list[dict] = [
        {"type": "TextBlock", "text": msg.title, "weight": "Bolder", "size": "Medium", "wrap": True,
         "color": TEAMS_COLORS.get(msg.tone, "Default")},
        {"type": "TextBlock", "text": msg.text, "wrap": True},
    ]
    if msg.facts:
        body.append({"type": "FactSet", "facts": [{"title": str(k), "value": str(v)} for k, v in msg.facts[:12]]})
    body.append({"type": "TextBlock", "text": footer(msg), "isSubtle": True, "size": "Small", "wrap": True})
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
        "msteams": {"width": "Full"},
    }
    if msg.url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": msg.link_label, "url": msg.url}]
    return {"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}


def email_parts(msg: Message) -> tuple[str, str, str]:
    """Subject, plain text and HTML for an email channel."""
    ctx = {"m": msg, "footer": footer(msg), "color": EMAIL_COLORS.get(msg.tone, EMAIL_COLORS["info"])}
    subject = _cut(f"[ShipMatch] {msg.title}", 200).replace("\n", " ")
    text = render_to_string("notifications/email/alert.txt", ctx)
    html = render_to_string("notifications/email/alert.html", ctx)
    return subject, text, html
