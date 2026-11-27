"""Structured, validated UK delivery address for physical SIM fulfilment.

CTO Response v2.0 §11.1 and Addendum v2.1 B5:
* the replacement SIM ships only to a validated, structured UK address;
* the postcode is normalised (e.g. "sw1a1aa" → "SW1A 1AA");
* a new address, or an account address changed recently, gets enhanced
  review (risk flag) rather than automatic dispatch. The review window is a
  proposed control (`SIM_CHANGE_ADDRESS_REVIEW_DAYS`) for Security to confirm.

The snapshot is stored encrypted on the case; events and logs never contain
the address itself.
"""

import re

from django.conf import settings
from django.db import transaction

from apps.security.models import AccountSecurityEvent
from apps.security.services import recent_changes

from ..models import PhysicalFulfilment, SimTypeChangeCase, SimTypeChangeEvent, StatusSource
from . import crypto
from .transitions import TransitionError

S = SimTypeChangeCase.Status

# UK postcode (Royal Mail format), incl. GIR 0AA.
_POSTCODE = re.compile(
    r"^(GIR ?0AA|[A-PR-UWYZ](?:[0-9]{1,2}|[A-HK-Y][0-9]{1,2}|[0-9][A-HJKPS-UW]|[A-HK-Y][0-9][ABEHMNPRV-Y])"
    r" ?[0-9][ABD-HJLNP-UW-Z]{2})$"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
LIMITS = {"line1": 100, "line2": 100, "town": 60, "county": 60}

REVIEW_FLAG = "DELIVERY_ADDRESS_REVIEW"


class AddressError(TransitionError):
    def __init__(self, errors: dict):
        super().__init__("INVALID_ADDRESS", "Check the delivery address.", 422)
        self.errors = errors


def normalise_postcode(value: str) -> str | None:
    compact = re.sub(r"\s+", "", str(value or "")).upper()
    if not _POSTCODE.match(compact):
        return None
    return f"{compact[:-3]} {compact[-3:]}"


def normalise_uk_address(data: dict) -> dict:
    """Validate and normalise; raises AddressError with per-field messages."""
    data = data or {}
    errors, clean = {}, {}
    for key, limit in LIMITS.items():
        value = re.sub(r"\s+", " ", str(data.get(key) or "")).strip()
        if _CONTROL.search(value):
            errors[key] = "Contains characters that aren't allowed."
        elif len(value) > limit:
            errors[key] = f"Keep this under {limit} characters."
        clean[key] = value
    if not clean["line1"] and "line1" not in errors:
        errors["line1"] = "Enter the first line of the address."
    if not clean["town"] and "town" not in errors:
        errors["town"] = "Enter the town or city."

    postcode = normalise_postcode(data.get("postcode"))
    if postcode is None:
        errors["postcode"] = "Enter a valid UK postcode."
    clean["postcode"] = postcode or ""

    country = str(data.get("country") or "GB").strip().upper()
    if country not in ("GB", "UK"):
        errors["country"] = "We can only deliver to a UK address."
    clean["country"] = "GB"

    if errors:
        raise AddressError(errors)
    return clean


def address_fingerprint(clean: dict) -> str:
    canonical = "|".join(clean[k].lower() for k in ("line1", "line2", "town", "postcode"))
    return crypto.text_fingerprint(canonical)


def review_days() -> int:
    return int(getattr(settings, "SIM_CHANGE_ADDRESS_REVIEW_DAYS", 14))


def set_delivery_address(case: SimTypeChangeCase, data: dict, *, source: str, actor=None,
                         status_source: str = StatusSource.CUSTOMER) -> SimTypeChangeCase:
    """Store the validated delivery address on a physical-SIM case.

    `source` is "on_file" (the account's existing address) or "new". A new
    address, or a recent account address change, adds the review flag.
    """
    if source not in SimTypeChangeCase.AddressSource.values or not source:
        raise TransitionError("INVALID_ADDRESS_SOURCE", "Unknown address source.", 422)
    clean = normalise_uk_address(data)

    with transaction.atomic():
        case = SimTypeChangeCase.objects.select_for_update().get(pk=case.pk)
        if case.to_type != "psim":
            raise TransitionError("NOT_PHYSICAL", "Only a physical SIM needs a delivery address.", 409)
        if not case.is_open or case.supplier_submitted or case.physical_fulfilment not in (
            PhysicalFulfilment.NOT_STARTED, PhysicalFulfilment.AWAITING_DISPATCH,
        ):
            raise TransitionError("ADDRESS_LOCKED", "The delivery address can't be changed at this stage.", 409)
        if case.status == S.HOLD_FRAUD:
            raise TransitionError("CASE_ON_HOLD", "This request is on hold for a security review.", 409)

        recent = recent_changes(case.owner, review_days())
        needs_review = source == SimTypeChangeCase.AddressSource.NEW or recent.changed(
            AccountSecurityEvent.Kind.DELIVERY_ADDRESS_CHANGED
        )
        flags = [f for f in case.risk_flags if f != REVIEW_FLAG]
        if needs_review:
            flags.append(REVIEW_FLAG)

        case.delivery_snapshot_encrypted = crypto.encrypt_json(clean)
        case.delivery_address_fingerprint = address_fingerprint(clean)
        case.delivery_address_source = source
        case.risk_flags = flags
        case.version += 1
        case.save()
        SimTypeChangeEvent.objects.create(
            tenant=case.tenant, case=case, actor=actor, kind="delivery_address_set", status_source=status_source,
            version=case.version,
            detail={"source": source, "review": needs_review, "history": recent.history_status},
        )
    return case


def delivery_address(case: SimTypeChangeCase) -> dict | None:
    """Decrypted address for dispatch staff; never put it in logs or events."""
    return crypto.decrypt_json(case.delivery_snapshot_encrypted)
