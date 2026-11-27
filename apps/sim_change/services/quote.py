"""Read-only quote for a SIM type change (CTO v2.0 §8, Addendum v2.1 §4.5).

No price is invented: until Commercial approves direction-specific fees and
`SIM_CHANGE_FEES_ENABLED` is on, the charge is "not shown" and nothing is
collected. A quote reserves no stock and creates no payment.

The quote is signed: `quote_hash` is a keyed fingerprint of its content plus
the user, line and expiry. Create / confirm recompute it and must match, so
the customer only ever agrees to what they were shown.
"""

import json
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import crypto
from .transitions import TransitionError

QUOTE_VERSION = "2026-11-27"
CHARGE_NOT_SHOWN = {
    "status": "NOT_SHOWN",
    "amount": None,
    "currency": "GBP",
    "message": "Any applicable charge will be shown before you confirm.",
}

ESIM_DEVICE_CHECKS = ("device_esim_compatible", "device_unlocked", "wifi_available")
PSIM_DEVICE_CHECKS = ("device_takes_physical_sim",)

REQUIREMENTS = {
    "esim": [
        "Check your phone supports eSIM and is unlocked.",
        "Have reliable Wi-Fi when you install the eSIM.",
        "Keep another way to contact us in case service is briefly interrupted.",
    ],
    "psim": [
        "We'll send a new SIM card to your UK delivery address.",
        "Tell us when it arrives; your current SIM keeps working until the change is made.",
        "Keep another way to contact us in case service is briefly interrupted.",
    ],
}


class QuoteError(TransitionError):
    pass


def device_problems(to_type: str, device: dict) -> list:
    checks = ESIM_DEVICE_CHECKS if to_type == "esim" else PSIM_DEVICE_CHECKS
    return [c for c in checks if (device or {}).get(c) is not True]


def _content(assessment, expires_at: str) -> dict:
    return {
        "quote_version": QUOTE_VERSION,
        "line_id": str(assessment.line.uuid),
        "target_type": assessment.target_type,
        "direction": assessment.direction,
        "charge": CHARGE_NOT_SHOWN,
        "delivery": (
            {"method": "courier", "tracking": "provided when dispatched", "sla": None}
            if assessment.target_type == "psim" else None
        ),
        "requirements": REQUIREMENTS[assessment.target_type],
        "expires_at": expires_at,
    }


def _sign(user, content: dict) -> str:
    canonical = json.dumps({"user": user.pk, **content}, sort_keys=True, separators=(",", ":"))
    return crypto.text_fingerprint(canonical)


def build_quote(user, assessment, *, device: dict) -> dict:
    missing = device_problems(assessment.target_type, device)
    if missing:
        raise QuoteError("DEVICE_NOT_READY", "Please confirm your device is ready for this change.", 422)
    minutes = int(getattr(settings, "SIM_CHANGE_QUOTE_MINUTES", 30))
    expires_at = (timezone.now() + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
    content = _content(assessment, expires_at)
    return {**content, "quote_hash": _sign(user, content)}


def verify_quote(user, assessment, submitted: dict) -> dict:
    """Recompute the quote the customer accepted; raise if it changed or expired."""
    submitted = submitted or {}
    expires_raw = str(submitted.get("expires_at") or "")
    expires_at = parse_datetime(expires_raw)
    if expires_at is None or not submitted.get("quote_hash"):
        raise QuoteError("QUOTE_REQUIRED", "Get a quote before submitting the request.", 422)
    if expires_at <= timezone.now():
        raise QuoteError("QUOTE_EXPIRED", "This quote has expired. Please review the latest details.", 412)
    content = _content(assessment, expires_raw)
    expected = _sign(user, content)
    if not crypto.same_text(expected, str(submitted["quote_hash"])):
        raise QuoteError("QUOTE_CHANGED", "The details have changed. Please review the latest quote.", 422)
    return {**content, "quote_hash": expected}
