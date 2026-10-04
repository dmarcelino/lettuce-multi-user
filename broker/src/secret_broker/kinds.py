"""The kinds of secret the vault holds. Kept deliberately short: agents read
natural language, so most kinds are one private text box. One table drives the
add/edit form, which fields agents can read without asking (public) and which
need approval (private), the preview on the approval page, and whether a secret
may be shared without asking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    # Private: hidden on screen until "Show", and shared with agents only on
    # approval (or without asking, where the kind allows it). Public: agents
    # read it in list_secrets, like the name.
    sensitive: bool = False
    required: bool = False
    multiline: bool = False
    hint: str = ""


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    fields: tuple[FieldSpec, ...]
    # Whether the person may choose "Agents may use it without asking".
    auto_allowed: bool = False


def _text(hint: str) -> FieldSpec:
    return FieldSpec("text", "Text", sensitive=True, required=True, multiline=True, hint=hint)


NOTES = FieldSpec("notes", "Notes", sensitive=True, multiline=True)

KINDS: dict[str, Kind] = {
    k.key: k
    for k in (
        Kind("note", "Note", (_text("anything you want to keep private"),), auto_allowed=True),
        Kind(
            "address",
            "Address",
            (_text("e.g. name, street, postal code, city, country, phone"),),
            auto_allowed=True,
        ),
        Kind(
            "company",
            "Company",
            (_text("e.g. company name, VAT ID, registration number, address"),),
            auto_allowed=True,
        ),
        Kind(
            "membership",
            "Membership / loyalty",
            (
                FieldSpec("programme", "Programme", hint="e.g. TAP Miles&Go"),
                FieldSpec("member_number", "Member number", sensitive=True, required=True),
                NOTES,
            ),
            auto_allowed=True,
        ),
        Kind(
            "id_document",
            "ID document",
            (
                FieldSpec("document_type", "Document type", hint="e.g. Passport, ID card, Driving licence"),
                FieldSpec("number", "Document number", sensitive=True, required=True),
                NOTES,
            ),
        ),
        Kind(
            "card",
            "Payment card",
            (
                FieldSpec("cardholder", "Name on card"),
                FieldSpec("number", "Card number", sensitive=True, required=True),
                FieldSpec("expiry", "Expiry (MM/YY)"),
                FieldSpec("security_code", "Security code", sensitive=True),
                NOTES,
            ),
        ),
        Kind(
            "login",
            "Login",
            (
                FieldSpec("website", "Website"),
                FieldSpec("username", "Username or email"),
                FieldSpec("password", "Password", sensitive=True, required=True),
            ),
        ),
    )
}


def _tail(value: str, n: int = 4) -> str:
    compact = re.sub(r"\s+", "", value)
    return compact[-n:] if len(compact) > n else ""


def preview(kind: str, fields: list[Any]) -> str:
    """Enough to tell which secret it is, without revealing anything private."""
    v = {f.key: f.value for f in fields}
    if kind == "card":
        tail = _tail(v.get("number", ""))
        return f"ending {tail}" if tail else ""
    if kind == "id_document":
        tail = _tail(v.get("number", ""), 2)
        return " ".join(x for x in (v.get("document_type", ""), f"ending {tail}" if tail else "") if x)
    if kind == "login":
        return " at ".join(x for x in (v.get("username", ""), v.get("website", "")) if x)
    if kind == "membership":
        return v.get("programme", "")
    return ""
