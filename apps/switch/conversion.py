"""Milestone 4, step B3: turn a paid order back into the switch request's progress.

Listens to add_line's signals (connected in SwitchConfig.ready):

* order_paid   → the request becomes CONVERTED ("Order created"). This is
  the only way a request is converted, so "converted" always means a real,
  paid order exists (T16/T20). The customer's tracker then shows
  "Order placed"; later steps still come only from the line and port.
* line_created → the new line is linked to the request and the request's
  encrypted PAC/STAC is handed to add_line's number transfer, which submits
  it to Transatel once the line is live. The copy on the request is then
  removed, so the code is held in one place only.

Receivers never raise into payment or fulfilment (send_robust), and a
problem here leaves a note for ops instead of blocking activation.
"""

import logging

from django.utils import timezone

from apps.add_line.services import pac, porting

from . import receipts, services
from .models import SwitchRequest, SwitchStatusEvent

logger = logging.getLogger("apps.switch")

S = SwitchRequest.Status


def _note(switch, text):
    stamp = timezone.now().strftime("%Y-%m-%d %H:%M")
    switch.ops_note = f"{switch.ops_note}\n[{stamp}] {text}".strip()
    switch.save(update_fields=["ops_note", "updated_at"])


def on_order_paid(sender, order, **kwargs):
    switch = SwitchRequest.objects.filter(order=order).first()
    if switch is None or switch.status == S.CONVERTED:
        return
    was = switch.status
    switch.set_status(S.CONVERTED)
    if was != S.READY_FOR_ORDER:
        # Paid while ops had moved the request on: the payment is honoured.
        _note(switch, f"Order {order.order_reference} paid while the request was '{was}'. Check it.")
    # Receipt (B4). Best effort here; reconcile_switch_receipts retries.
    try:
        receipts.record_payment(order)
    except Exception:
        logger.exception("Recording the payment/receipt for %s failed; reconcile will retry",
                         order.order_reference)


def on_line_created(sender, order, line, **kwargs):
    switch = SwitchRequest.objects.filter(order=order).first()
    if switch is None:
        return
    if switch.line_id != line.pk:
        switch.line = line
        switch.save(update_fields=["line", "updated_at"])

    code_type = switch.required_code_type
    if not code_type:
        return
    if not switch.encrypted_code:
        # Customer chose "send it later": they add it from their account.
        return
    if services.code_expired(switch):
        _note(switch, f"{code_type} expired before the line was created; customer must send a new one.")
        return
    try:
        code = pac.decrypt(switch.encrypted_code)
        porting.create_request(order.owner, line, code_type=code_type, code=code, msisdn=switch.port_number)
    except (porting.PortError, pac.PacStorageUnavailable, pac.PacError, ValueError) as exc:
        logger.error("Handing %s to porting failed for %s: %s", code_type, switch.reference, exc)
        _note(switch, f"Could not start the {code_type} transfer automatically: {getattr(exc, 'code', exc)}.")
        return

    switch.encrypted_code = ""
    switch.save(update_fields=["encrypted_code", "updated_at"])
    SwitchStatusEvent.objects.create(
        switch_request=switch, kind=SwitchStatusEvent.Kind.CODE_TRANSFERRED,
        status_before=switch.status, status_after=switch.status, note=f"line {line.uuid}",
    )


def connect():
    from apps.add_line import signals

    signals.order_paid.connect(on_order_paid, dispatch_uid="switch.on_order_paid")
    signals.line_created.connect(on_line_created, dispatch_uid="switch.on_line_created")
    signals.charge_event.connect(receipts.on_charge_event, dispatch_uid="switch.on_charge_event")
