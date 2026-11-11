"""Switch & Save logic shared by the serializers, views and admin.

* Codes: format-checked and encrypted with the add_line PAC helpers.
* Status: milestones 1–7, PAC state and the estimated completion date are
  derived from the request and, once linked, its order / line / port request,
  so they never drift from the real state.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.add_line.models import AddLineOrder, Line, PortRequest
from apps.add_line.services import pac

from .models import SwitchRequest, SwitchStatusEvent

PacError = pac.PacError
CodeStorageUnavailable = pac.PacStorageUnavailable

# Working days from "code received" (or submission when no code is needed)
# to completion, used when ops haven't set a date. UK bank holidays ignored.
DEFAULT_ESTIMATE_WORKING_DAYS = 3

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
_PORT_IN_PROGRESS = (
    PortRequest.Status.PENDING, PortRequest.Status.ATTACH_REQUESTED,
    PortRequest.Status.ATTACH_RECEIVED, PortRequest.Status.VALIDATED,
    PortRequest.Status.LOCKED, PortRequest.Status.DONE,
)


# ── Codes ─────────────────────────────────────────────────────────────────────

def normalise_code(code_type: str, code: str) -> str:
    """Raises PacError (code + customer message) if the format is wrong."""
    return pac.normalise_code(code_type, code)


def normalise_msisdn(number: str) -> str:
    return pac.normalise_msisdn(number)


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


def record_code_received(switch: SwitchRequest, actor=None) -> None:
    SwitchStatusEvent.objects.create(
        switch_request=switch, kind=SwitchStatusEvent.Kind.CODE_RECEIVED,
        status_before=switch.status, status_after=switch.status,
        actor=actor if actor is not None and actor.is_authenticated else None,
    )


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
        return "received"
    if switch.code_purged_at:
        return "expired"
    return "awaiting_code"


def _first_event_at(switch: SwitchRequest, **filters):
    event = switch.events.filter(**filters).order_by("created_at").first()
    return event.created_at if event else None


def milestones(switch: SwitchRequest) -> list[dict]:
    S = SwitchRequest.Status
    port = _latest_port(switch)
    order = switch.order if switch.order_id else None
    line = switch.line if switch.line_id else None
    completed = switch.status == S.COMPLETED

    reviewed_at = _first_event_at(switch, kind=SwitchStatusEvent.Kind.STATUS_CHANGED)
    no_code = switch.switch_route == SwitchRequest.Route.NEW_KEEP
    code_at = switch.created_at if no_code else switch.code_received_at
    paid_at = order.paid_at if order and order.payment_status == AddLineOrder.PaymentStatus.PAID else None
    active_at = line.activated_at if line and line.status == Line.Status.ACTIVE else None
    port_at = port.submitted_at if port and port.status in _PORT_IN_PROGRESS else None
    if no_code and line and line.status == Line.Status.ACTIVE:
        port_at = active_at  # nothing to transfer

    steps = [
        ("received", "Request received", switch.created_at, True),
        ("under_review", "Under review", reviewed_at, switch.status != S.PENDING),
        ("code_received", "No code needed" if no_code else "Switching code received", code_at, bool(code_at)),
        ("payment", "Order and payment", paid_at, bool(paid_at)),
        ("sim_activated", "SIM activated", active_at, bool(active_at)),
        ("transfer", "Number transfer in progress", port_at, bool(port_at)),
        ("completed", "Switch complete", switch.completed_at, completed),
    ]
    return [
        {"step": i, "key": key, "label": label, "done": done or completed, "at": at if done else None}
        for i, (key, label, at, done) in enumerate(steps, start=1)
    ]


def current_milestone(steps: list[dict]) -> int:
    """Highest step reached (1–7)."""
    return max(s["step"] for s in steps if s["done"])


def _add_working_days(start, days: int):
    current = start
    while days > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days -= 1
    return current


def estimated_completion_date(switch: SwitchRequest):
    """Ops override, else a working-day estimate. None when closed or waiting on a code."""
    if switch.status == SwitchRequest.Status.COMPLETED and switch.completed_at:
        return timezone.localdate(switch.completed_at)
    if switch.status in (SwitchRequest.Status.FAILED, SwitchRequest.Status.CANCELLED):
        return None
    if switch.estimated_completion_date:
        return switch.estimated_completion_date
    port = _latest_port(switch)
    if port and port.port_date:
        return port.port_date
    if switch.switch_route == SwitchRequest.Route.NEW_KEEP:
        start = switch.created_at
    else:
        start = switch.code_received_at
    if not start:
        return None
    days = getattr(settings, "SWITCH_ESTIMATE_WORKING_DAYS", DEFAULT_ESTIMATE_WORKING_DAYS)
    return _add_working_days(timezone.localdate(start), days)


def history(switch: SwitchRequest) -> list[dict]:
    labels = dict(SwitchRequest.Status.choices)
    out = []
    for event in switch.events.all():
        if event.kind == SwitchStatusEvent.Kind.CODE_RECEIVED:
            label = "Switching code received"
        else:
            label = labels.get(event.status_after, event.status_after)
        out.append({"event": event.kind, "status": event.status_after, "label": label, "at": event.created_at})
    return out


def mask_number(msisdn: str) -> str:
    """447700900123 → 07*** ***123"""
    if not msisdn:
        return ""
    local = "0" + msisdn[2:] if msisdn.startswith("44") else msisdn
    return local[:2] + "*** ***" + local[-3:]
