"""Switch & Save logic shared by the serializers, views, admin and commands.

* Codes: format-checked and encrypted with the add_line PAC helpers.
* Tracker: built only from the linked order, line and port request (the
  authoritative records), never from the request status, so a request can't
  be shown as an order, activation or transfer it isn't (CTO M04 §03, T16).
* Switching date: only a date that is actually known (supplier port date or
  one confirmed by ops). No calculated promises (M04 §06/§07).
"""

import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.add_line.models import AddLineOrder, Line, PortRequest
from apps.add_line.services import pac

from .models import SwitchRequest, SwitchStatusEvent

PacError = pac.PacError
CodeStorageUnavailable = pac.PacStorageUnavailable

S = SwitchRequest.Status
R = SwitchRequest.ActionReason

DEFAULT_DEDUPE_MINUTES = 10

# A PAC/STAC is valid for 30 days from issue (Ofcom). We only know when we
# received it, so this is the latest it can still be valid.
CODE_LIFE_DAYS = 30

NO_DATE_MESSAGE = (
    "Your switching date will be confirmed once the required checks and "
    "service arrangements are complete."
)

_PORT_TO_PAC_STATUS = {
    PortRequest.Status.AWAITING_ACTIVATION: "received",
    PortRequest.Status.PENDING: "submitted",
    PortRequest.Status.ATTACH_REQUESTED: "submitted",
    PortRequest.Status.ATTACH_RECEIVED: "submitted",
    PortRequest.Status.VALIDATED: "validated",
    PortRequest.Status.LOCKED: "validated",
    PortRequest.Status.DONE: "completed",
    PortRequest.Status.ATTACH_FAILED: "failed",
    PortRequest.Status.INVALIDATED: "failed",
    PortRequest.Status.ERROR: "failed",
    PortRequest.Status.CANCELING: "cancelled",
    PortRequest.Status.CANCELED: "cancelled",
}
# The supplier has accepted the transfer and a date is (or will be) set.
_PORT_SCHEDULED = (PortRequest.Status.VALIDATED, PortRequest.Status.LOCKED, PortRequest.Status.DONE)


# ── Codes ─────────────────────────────────────────────────────────────────────

def normalise_code(code_type: str, code: str) -> str:
    """Raises PacError (code + customer message) if the format is wrong."""
    return pac.normalise_code(code_type, code)


def normalise_msisdn(number: str) -> str:
    return pac.normalise_msisdn(number)


UK_POSTCODE_RE = re.compile(r"^(GIR 0AA|[A-Z]{1,2}[0-9][A-Z0-9]? [0-9][A-Z]{2})$")


def normalise_postcode(value: str) -> str:
    """"sw1a1aa" / " SW1A  1AA " → "SW1A 1AA". Raises ValueError if not a UK postcode."""
    compact = re.sub(r"\s+", "", value or "").upper()
    formatted = f"{compact[:-3]} {compact[-3:]}" if len(compact) > 3 else compact
    if not UK_POSTCODE_RE.match(formatted):
        raise ValueError("Enter a valid UK postcode.")
    return formatted


def apply_code(switch: SwitchRequest, code_type: str, code: str, msisdn: str = "") -> None:
    """Encrypt and attach a (normalised) code. The caller saves the instance.

    Raises CodeStorageUnavailable when PAC_ENCRYPTION_KEY isn't configured.
    """
    switch.encrypted_code = pac.encrypt(code)
    switch.code_type = code_type
    switch.code_received_at = timezone.now()
    switch.code_purged_at = None
    switch.pac_later = False
    if msisdn:
        switch.port_number = msisdn


def code_expiry_cutoff(now=None):
    """Codes received before this are past their 30-day life."""
    return (now or timezone.now()) - timedelta(days=CODE_LIFE_DAYS)


def code_expired(switch: SwitchRequest, now=None) -> bool:
    """The held code is past its 30-day life. Separate from data retention."""
    return bool(
        switch.encrypted_code and switch.code_received_at
        and switch.code_received_at <= code_expiry_cutoff(now)
    )


def record_code_received(switch: SwitchRequest, actor=None) -> None:
    SwitchStatusEvent.objects.create(
        switch_request=switch, kind=SwitchStatusEvent.Kind.CODE_RECEIVED,
        status_before=switch.status, status_after=switch.status,
        actor=actor if actor is not None and actor.is_authenticated else None,
    )


# ── Intake checks ─────────────────────────────────────────────────────────────

def initial_status(switch: SwitchRequest):
    """(status, reason) after the automated checks at submit (M04 stage 2)."""
    if switch.required_code_type and not switch.encrypted_code:
        return S.ACTION_NEEDED, R.CODE_NEEDED
    return S.VALIDATING, ""


def find_duplicate(*, email, mobile, plan_id, contract_term, sim_type, switch_route):
    """An identical open request submitted in the last few minutes (T03)."""
    minutes = getattr(settings, "SWITCH_DEDUPE_MINUTES", DEFAULT_DEDUPE_MINUTES)
    return (
        SwitchRequest.objects.filter(
            email__iexact=email, mobile=mobile, plan_id=plan_id, contract_term=contract_term,
            sim_type=sim_type, switch_route=switch_route,
            created_at__gte=timezone.now() - timedelta(minutes=minutes),
        )
        .exclude(status__in=SwitchRequest.TERMINAL_STATUSES)
        .order_by("-created_at")
        .first()
    )


def email_has_account(email: str) -> bool:
    return get_user_model().objects.filter(email__iexact=email.strip()).exists()


# ── Derived status ────────────────────────────────────────────────────────────

def _latest_port(switch: SwitchRequest):
    if not switch.line_id:
        return None
    return switch.line.port_requests.order_by("-created_at").first()


def pac_status(switch: SwitchRequest) -> str:
    """not_required | awaiting_code | received | expired | submitted |
    validated | completed | failed | cancelled"""
    port = _latest_port(switch)
    if port:
        return _PORT_TO_PAC_STATUS.get(port.status, "submitted")
    if switch.switch_route == SwitchRequest.Route.NEW_KEEP:
        return "not_required"
    if switch.encrypted_code:
        return "expired" if code_expired(switch) else "received"
    if switch.code_purged_at:
        return "expired"
    return "awaiting_code"


def customer_status(switch: SwitchRequest):
    """(status, label) shown to the customer.

    An action for Operations is shown as "Checking your request", so the
    customer only sees "Action needed" when they have to do something.
    """
    if switch.status == S.ACTION_NEEDED and not switch.needs_customer:
        return S.VALIDATING, S.VALIDATING.label
    return switch.status, switch.get_status_display()


def tracker(switch: SwitchRequest) -> dict:
    """Customer tracker: progress steps plus the current stage.

    Stage keys follow the approved wording (M04 §06): request_received,
    action_needed, order_placed, sim_ready, switch_scheduled,
    number_transferred.
    """
    order = switch.order if switch.order_id else None
    line = switch.line if switch.line_id else None
    port = _latest_port(switch)
    route = switch.switch_route

    placed_at = order.paid_at if order and order.payment_status == AddLineOrder.PaymentStatus.PAID else None
    ready_at = line.activated_at if line and line.status == Line.Status.ACTIVE else None
    scheduled_at = port.validated_at if port and port.status in _PORT_SCHEDULED else None
    done_at = port.completed_at if port and port.status == PortRequest.Status.DONE else None

    has_transfer = route != SwitchRequest.Route.NEW_KEEP
    final_label = "Old service closed" if route == SwitchRequest.Route.NEW_STAC else "Number transferred"
    rows = [
        ("request_received", "Request received", switch.created_at, True),
        ("order_placed", "Order placed", placed_at, True),
        ("sim_ready", "SIM ready", ready_at, True),
        ("switch_scheduled", "Switch scheduled", scheduled_at, has_transfer),
        ("number_transferred", final_label, done_at, has_transfer),
    ]
    steps = [
        {"key": key, "label": label, "applicable": applicable,
         "done": bool(at) and applicable, "at": at if applicable else None}
        for key, label, at, applicable in rows
    ]

    if switch.needs_customer:
        stage = {"key": "action_needed", "label": "Action needed"}
    else:
        last = [s for s in steps if s["done"]][-1]
        stage = {"key": last["key"], "label": last["label"]}
    return {"stage": stage, "steps": steps}


def action_needed(switch: SwitchRequest):
    """Customer-facing {reason, message} or None. Internal notes and
    Operations-only actions never leave."""
    if not switch.needs_customer:
        return None
    code = switch.required_code_type or "switching code"
    messages = {
        R.CODE_NEEDED: f"Please send us your {code} so we can continue your switch.",
        R.CODE_INVALID: (
            f"Your current provider didn't accept the {code}. Please ask them for a new "
            f"one and send it to us."
        ),
        R.CODE_EXPIRED: f"Your {code} has expired. Please ask your current provider for a new one.",
    }
    message = messages.get(
        switch.action_reason,
        "We need some more information from you. We'll contact you with the next steps.",
    )
    return {"reason": switch.action_reason, "message": message}


def next_action(switch: SwitchRequest) -> str:
    """provide_code | await_contact | none"""
    if not switch.needs_customer:
        return "none"
    if switch.action_reason in SwitchRequest.CODE_REASONS and switch.required_code_type:
        return "provide_code"
    return "await_contact"


def switch_date(switch: SwitchRequest):
    """A date that is actually known, else None (show NO_DATE_MESSAGE)."""
    if switch.status in SwitchRequest.TERMINAL_STATUSES:
        return None
    port = _latest_port(switch)
    if port and port.port_date:
        return port.port_date
    return switch.confirmed_switch_date


def history(switch: SwitchRequest) -> list[dict]:
    """Customer-facing history. Operations-only actions read as "Checking
    your request", and repeats of the same step are shown once."""
    labels = dict(S.choices)
    out = []
    for event in switch.events.all():
        status = event.status_after
        if event.kind == SwitchStatusEvent.Kind.CODE_RECEIVED:
            label = "Switching code received"
        else:
            if status == S.ACTION_NEEDED and event.action_owner != SwitchRequest.ActionOwner.CUSTOMER:
                status = S.VALIDATING
            label = labels.get(status, status)
            if out and out[-1]["event"] != SwitchStatusEvent.Kind.CODE_RECEIVED and out[-1]["status"] == status:
                continue
        out.append({"event": event.kind, "status": status, "label": label, "at": event.created_at})
    return out


# ── Masking (emails, admin lists, API) ────────────────────────────────────────

def mask_number(msisdn: str) -> str:
    """447700900123 / 07700900123 → 07*** ***123"""
    if not msisdn:
        return ""
    digits = msisdn.lstrip("+")
    local = "0" + digits[2:] if digits.startswith("44") else digits
    return local[:2] + "*** ***" + local[-3:]


def mask_email(email: str) -> str:
    """jane.doe@example.com → j***@example.com"""
    if not email or "@" not in email:
        return ""
    name, domain = email.split("@", 1)
    return f"{name[:1]}***@{domain}"
