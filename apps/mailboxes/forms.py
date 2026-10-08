from __future__ import annotations

import re

from django import forms

from .models import Mailbox

_RULE = re.compile(r"^(?:[^@\s]+@|@)?(?:\*\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$")


def clean_sender_rules(text: str) -> str:
    rules = [r.strip().lower() for r in re.split(r"[\s,;]+", text or "") if r.strip()]
    bad = [r for r in rules if not _RULE.match(r)]
    if bad:
        raise forms.ValidationError(
            f"These aren't email addresses or domains: {', '.join(bad[:5])}. Use one per line, for example "
            "billing@harborlink.com or harborlink.com.")
    if len(rules) > 200:
        raise forms.ValidationError("Use at most 200 addresses or domains.")
    return "\n".join(dict.fromkeys(rules))


class SendersMixin:
    def clean_allowed_senders(self):
        return clean_sender_rules(self.cleaned_data.get("allowed_senders", ""))


class InboundForm(SendersMixin, forms.Form):
    allowed_senders = forms.CharField(required=False, max_length=8000)


class ImapForm(SendersMixin, forms.Form):
    display_name = forms.CharField(max_length=120, required=False)
    host = forms.CharField(max_length=255)
    port = forms.IntegerField(min_value=1, max_value=65535, initial=993)
    security = forms.ChoiceField(choices=Mailbox.Security.choices, initial=Mailbox.Security.SSL)
    username = forms.CharField(max_length=320)
    password = forms.CharField(max_length=500, required=False, strip=False)
    folder = forms.CharField(max_length=255, required=False, initial="INBOX")
    mark_seen = forms.BooleanField(required=False)
    allowed_senders = forms.CharField(required=False, max_length=8000)

    def __init__(self, *args, require_password: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.require_password = require_password

    def clean_host(self):
        host = self.cleaned_data["host"].strip().lower()
        host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", host).split("/")[0]
        if host.count(":") == 1:
            host = host.split(":")[0]
        if not _HOST.match(host) or "." not in host:
            raise forms.ValidationError("Enter the server name only, for example imap.example.com.")
        return host

    def clean_folder(self):
        return (self.cleaned_data.get("folder") or "").strip() or "INBOX"

    def clean_password(self):
        password = self.cleaned_data.get("password") or ""
        if self.require_password and not password:
            raise forms.ValidationError("Enter the password (or app password) for this mailbox.")
        return password


class MicrosoftForm(SendersMixin, forms.Form):
    display_name = forms.CharField(max_length=120, required=False)
    folder = forms.ChoiceField(choices=())
    after_import = forms.ChoiceField(choices=Mailbox.AfterImport.choices)
    processed_folder = forms.ChoiceField(choices=(), required=False)
    allowed_senders = forms.CharField(required=False, max_length=8000)

    def __init__(self, *args, mailbox: Mailbox, **kwargs):
        super().__init__(*args, **kwargs)
        options = [(f["id"], f["name"]) for f in mailbox.folder_options or []]
        if mailbox.folder and mailbox.folder not in {o[0] for o in options}:
            options.insert(0, (mailbox.folder, mailbox.folder_name or "Current folder"))
        self.names = dict(options)
        self.fields["folder"].choices = options
        self.fields["processed_folder"].choices = [("", "Choose a folder"), *options]

    def clean(self):
        data = super().clean()
        moves = data.get("after_import") in (Mailbox.AfterImport.MOVE, Mailbox.AfterImport.CATEGORY_MOVE)
        if moves and not data.get("processed_folder"):
            self.add_error("processed_folder", "Choose the folder imported emails should move to.")
        elif moves and data.get("processed_folder") == data.get("folder"):
            self.add_error("processed_folder", "Choose a different folder from the one ShipMatch reads.")
        return data


class GmailForm(SendersMixin, forms.Form):
    display_name = forms.CharField(max_length=120, required=False)
    folder = forms.CharField(max_length=255, label="Label")
    allowed_senders = forms.CharField(required=False, max_length=8000)

    def clean_folder(self):
        return self.cleaned_data["folder"].strip()
