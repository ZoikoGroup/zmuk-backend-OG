"""Customer-facing wording the backend owns, by version.

Each submitted request stores the exact sentence the customer ticked, so
wording is never edited in place: add a new version here and point
SWITCH_CONSENT_VERSION at it. The frontend shows the text it gets from the
API (`consent_statement`), not a copy of its own.
"""

from django.conf import settings
from django.core import checks

# Step 4 switching authorisation (Contract Recovery & Release decision v2.3 §4,
# 19 Nov 2026). Non-binding: no order, payment, activation or transfer.
REQUEST_AUTHORISATION = {
    "2026-11-20": (
        "I authorise Zoiko Mobile UK to process my switching request. I understand that this does not "
        "place a paid order, take a payment, activate a SIM or transfer my number. I will review and "
        "accept the final contract documents separately before completing an order."
    ),
}


def consent_version() -> str:
    return settings.SWITCH_CONSENT_VERSION


def request_authorisation(version: str | None = None) -> str:
    """The Step 4 sentence for `version` (default: the current one)."""
    return REQUEST_AUTHORISATION[version or consent_version()]


@checks.register()
def check_consent_wording(app_configs=None, **kwargs):
    if consent_version() not in REQUEST_AUTHORISATION:
        return [checks.Error(
            f"SWITCH_CONSENT_VERSION={consent_version()!r} has no wording in apps/switch/wording.py.",
            hint="Add the approved Step 4 sentence under that version, or set the setting to an existing one.",
            id="switch.E001",
        )]
    return []
