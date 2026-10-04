"""The kinds of item the vault holds. One table drives the add/edit form, which
fields are hidden until "Show", the preview on the approval page, and whether
an item may be shared with agents without asking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    sensitive: bool = False
    multiline: bool = False
    hint: str = ""


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    fields: tuple[FieldSpec, ...]
    # Low-risk kinds may be set to "Agents may use without asking".
    auto_allowed: bool = False


KINDS: dict[str, Kind] = {
    k.key: k
    for k in (
        Kind(
            "address",
            "Address",
            (
                FieldSpec("full_name", "Full name"),
                FieldSpec("street", "Street and number"),
                FieldSpec("city", "City"),
                FieldSpec("postal_code", "Postal code"),
                FieldSpec("region", "Region / state"),
                FieldSpec("country", "Country"),
                FieldSpec("phone", "Phone"),
            ),
            auto_allowed=True,
        ),
        Kind(
            "company",
            "Company / tax",
            (
                FieldSpec("company_name", "Company or name"),
                FieldSpec("vat_id", "VAT / tax ID"),
                FieldSpec("registration", "Registration number"),
                FieldSpec("address", "Address", multiline=True),
            ),
            auto_allowed=True,
        ),
        Kind(
            "membership",
            "Membership / loyalty",
            (
                FieldSpec("programme", "Programme", hint="e.g. TAP Miles&Go"),
                FieldSpec("member_number", "Member number"),
                FieldSpec("tier", "Tier / status"),
            ),
            auto_allowed=True,
        ),
        Kind(
            "card",
            "Payment card",
            (
                FieldSpec("cardholder", "Name on card"),
                FieldSpec("number", "Card number", sensitive=True),
                FieldSpec("expiry", "Expiry (MM/YY)"),
                FieldSpec("security_code", "Security code", sensitive=True),
            ),
        ),
        Kind(
            "id_document",
            "ID document",
            (
                FieldSpec("document_type", "Document type", hint="e.g. Passport, ID card, Driving licence"),
                FieldSpec("number", "Document number", sensitive=True),
                FieldSpec("full_name", "Full name"),
                FieldSpec("nationality", "Nationality"),
                FieldSpec("date_of_birth", "Date of birth"),
                FieldSpec("issued", "Issue date"),
                FieldSpec("expires", "Expiry date"),
            ),
        ),
        Kind(
            "login",
            "Login",
            (
                FieldSpec("website", "Website"),
                FieldSpec("username", "Username or email"),
                FieldSpec("password", "Password", sensitive=True),
            ),
        ),
        Kind("note", "Note", (FieldSpec("text", "Text", sensitive=True, multiline=True),)),
    )
}


def slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return s[:40] or "field"


def _tail(value: str, n: int = 4) -> str:
    digits = re.sub(r"\s+", "", value)
    return digits[-n:] if len(digits) > n else ""


def preview(kind: str, fields: list[Any]) -> str:
    """Enough to tell which item it is, without revealing anything sensitive."""
    v = {f.key: f.value for f in fields}
    if kind == "card":
        tail = _tail(v.get("number", ""))
        return f"ending {tail}" if tail else ""
    if kind == "id_document":
        tail = _tail(v.get("number", ""), 2)
        return " ".join(x for x in (v.get("document_type", ""), f"ending {tail}" if tail else "") if x)
    if kind == "login":
        return " at ".join(x for x in (v.get("username", ""), v.get("website", "")) if x)
    if kind == "address":
        return ", ".join(x for x in (v.get("city", ""), v.get("country", "")) if x)
    if kind == "membership":
        return v.get("programme", "")
    if kind == "company":
        return v.get("company_name", "")
    return ""
