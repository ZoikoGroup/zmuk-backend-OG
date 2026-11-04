"""Turn a paid add-line order into a live line.

`fulfil(order)` is idempotent and safe to call again after any failure:

    1. make sure the order owns a SIM (re-reserving if the cart hold lapsed);
    2. create the `sims.Order` mirror and the `Line` (pending activation),
       which takes over the slot from the capacity hold;
    3. activate the SIM on Transatel — only once: if the SIM is already marked
       ACTIVATED we only re-check its status, never send /activate or /modify
       again (that would add the plan package twice);
    4. the line becomes ACTIVE only when Transatel reports the subscriber live.

Failures are recorded on the order (`errors`, status FAILED) and retried by
`retry_add_line_fulfilment`, up to MAX_ATTEMPTS, after which support takes over.
"""

import logging

from django.db import transaction
from django.utils import timezone

from apps.sims import reservations
from apps.sims.esim import deliver_qr
from apps.sims.models import Order as SimOrder
from apps.sims.models import Sim, SimReservation
from apps.sims.psim import deliver_details
from apps.sims.transatel import TransatelError, get_service
from apps.sims.transatel.service import build_subscriber_info

from ..models import AddLineOrder, Line
from . import audit, capacity, porting
from .checkout import cart_key

logger = logging.getLogger("apps.add_line")

MAX_ATTEMPTS = 5
# Subscriber statuses that mean the line is live on the network.
_LIVE_STATUSES = {"active", "activated"}

Fulfilment = AddLineOrder.FulfilmentStatus


class FulfilmentError(Exception):
    pass


def attempts(order: AddLineOrder) -> int:
    return len(order.errors or [])


def _fail(order: AddLineOrder, step: str, exc: Exception):
    logger.error("Add-line fulfilment failed for %s at %s: %s", order.order_reference, step, exc)
    before = order.fulfilment_status
    order.errors = [*(order.errors or []), {"at": timezone.now().isoformat(), "step": step, "error": str(exc)}]
    order.fulfilment_status = Fulfilment.FAILED
    order.save(update_fields=["errors", "fulfilment_status", "updated_at"])
    audit.record("fulfilment_failed", order=order, line=order.line, before=before,
                 after=order.fulfilment_status, step=step, attempt=attempts(order))


def _secure_sim(order: AddLineOrder) -> Sim:
    """The SIM this order will activate, assigned to the order reference."""
    ref = order.order_reference
    sim = Sim.objects.select_for_update().filter(pk=order.reserved_sim_id).first()

    if sim and sim.order_reference == ref and sim.inventory in (
        Sim.Inventory.RESERVED, Sim.Inventory.ACTIVATED
    ):
        return sim
    if sim and sim.inventory == Sim.Inventory.PENDING and SimReservation.objects.filter(
        sim=sim, cart_key=cart_key(order)
    ).exists():
        return reservations.assign_to_order([sim.pk], ref)[0]

    # The cart hold lapsed (payment took longer than the SIM reservation TTL):
    # take a fresh SIM of the same type.
    try:
        sim_ids = reservations.reserve_sims(order.sim_type, 1, cart_key(order), session_id=ref)
    except reservations.InsufficientInventory as exc:
        raise FulfilmentError(f"no {order.sim_type} stock to replace lapsed reservation") from exc
    sim = reservations.assign_to_order(sim_ids, ref)[0]
    order.reserved_sim = sim
    order.save(update_fields=["reserved_sim", "updated_at"])
    return sim


def _subscriber_info(order: AddLineOrder) -> dict:
    return build_subscriber_info({"billing": order.billing, "email": order.owner.email})


def _ensure_sim_order(order: AddLineOrder) -> SimOrder:
    if order.sim_order_id:
        return order.sim_order
    sim_order, _ = SimOrder.objects.get_or_create(
        order_reference=order.order_reference,
        defaults={
            "email": order.owner.email,
            "billing_email": order.billing.get("email") or order.owner.email,
            # Set by the server from the authenticated owner, never the client.
            "user_id": str(order.owner_id),
            "country_code": "GB",
            "payload": {"source": "add_line", "plan_id": order.plan_id, "term": order.term},
            "subscriber": _subscriber_info(order),
        },
    )
    order.sim_order = sim_order
    order.save(update_fields=["sim_order", "updated_at"])
    return sim_order


def _ensure_line(order: AddLineOrder, sim: Sim) -> Line:
    if order.line_id:
        return order.line
    line = Line.objects.create(
        owner=order.owner, sim=sim, plan=order.plan, term=order.term,
        current_msisdn=sim.msisdn or "",
    )
    order.line = line
    order.save(update_fields=["line", "updated_at"])
    if order.hold_id:
        capacity.consume_hold(order.hold)
    audit.record("line_created", line=line, order=order, after=line.status)
    return line


def _mark_live(order: AddLineOrder, line: Line, sim_order: SimOrder):
    line_before = line.status
    line.status = Line.Status.ACTIVE
    line.activated_at = timezone.now()
    line.current_msisdn = line.current_msisdn or line.sim.msisdn
    line.save(update_fields=["status", "activated_at", "current_msisdn", "updated_at"])
    audit.record("line_activated", line=line, order=order, before=line_before, after=line.status)

    order.fulfilment_status = Fulfilment.ACTIVATED
    order.save(update_fields=["fulfilment_status", "updated_at"])
    sim_order.status = SimOrder.STATUS_COMPLETED
    sim_order.save(update_fields=["status", "updated_at"])


def _deliver(service, sim: Sim):
    """Best-effort QR / details email; the scheduled send_esim_qr and
    send_psim_details commands pick up anything missed."""
    try:
        deliver_qr(service, sim) if sim.is_esim else deliver_details(sim)
    except Exception:
        logger.warning("Inline SIM delivery failed for %s; scheduled job will retry", sim.iccid, exc_info=True)


def _activate(order: AddLineOrder, sim: Sim, service) -> str:
    """Send the activation once. Returns the subscriber status seen after."""
    subscriber_info = _subscriber_info(order)
    result = service.activate_order_sim(
        serial_number=sim.serial_number or sim.iccid,
        package_code=order.plan.transatelID,
        order_reference=order.order_reference,
        country_code="GB",
        subscriber_info=subscriber_info,
    )
    reservations.mark_activated(sim, transaction_id=result.get("transaction_id", ""))

    msisdn = result.get("msisdn") or sim.msisdn
    if msisdn and msisdn != sim.msisdn:
        sim.msisdn = msisdn
        sim.save(update_fields=["msisdn"])
    if order.line.current_msisdn != (msisdn or ""):
        order.line.current_msisdn = msisdn or ""
        order.line.save(update_fields=["current_msisdn", "updated_at"])

    contact = result.get("contact") or {}
    if contact.get("status") == "pending":
        # push_pending_contact completes this once the SIM goes Active.
        sim.pending_contact = subscriber_info
        sim.save(update_fields=["pending_contact"])

    audit.record("activation_sent", line=order.line, order=order,
                 transaction_id=result.get("transaction_id", ""),
                 post_status=result.get("post_status", ""))
    return result.get("post_status", "")


def fulfil(order: AddLineOrder) -> AddLineOrder:
    order = _fulfil_locked(order)
    if order.fulfilment_status == Fulfilment.ACTIVATED and order.line_id:
        # Outside the order lock: a porting problem must never undo activation.
        try:
            porting.submit_waiting(order.line)
        except Exception:
            logger.exception("Submitting waiting port request failed for %s", order.order_reference)
    return order


def _fulfil_locked(order: AddLineOrder) -> AddLineOrder:
    with transaction.atomic():
        order = AddLineOrder.objects.select_for_update().select_related("plan", "owner").get(pk=order.pk)
        if order.payment_status != AddLineOrder.PaymentStatus.PAID or order.fulfilment_status in (
            Fulfilment.ACTIVATED, Fulfilment.CANCELLED
        ):
            return order

        step = "sim"
        try:
            sim = _secure_sim(order)
            sim_order = _ensure_sim_order(order)
            line = _ensure_line(order, sim)

            step = "activation"
            service = get_service()
            if sim.inventory == Sim.Inventory.ACTIVATED:
                # Already sent on an earlier attempt: only check where it got to.
                subscriber = service.get_subscriber_by_serial(sim.serial_number or sim.iccid) or {}
                status = str(subscriber.get("status", "") or "")
            else:
                order.fulfilment_status = Fulfilment.ACTIVATING
                order.save(update_fields=["fulfilment_status", "updated_at"])
                status = _activate(order, sim, service)
        except (FulfilmentError, TransatelError) as exc:
            _fail(order, step, exc)
            return order

        if status.strip().lower() in _LIVE_STATUSES:
            _mark_live(order, line, sim_order)
            _deliver(service, sim)
        elif order.fulfilment_status != Fulfilment.ACTIVATING:
            # Activation sent earlier but still provisioning.
            order.fulfilment_status = Fulfilment.ACTIVATING
            order.save(update_fields=["fulfilment_status", "updated_at"])
    return order


def pending_retry():
    """Paid orders that still need work and haven't used up their attempts."""
    candidates = AddLineOrder.objects.filter(
        payment_status=AddLineOrder.PaymentStatus.PAID,
        fulfilment_status__in=[
            Fulfilment.NOT_STARTED, Fulfilment.SIM_RESERVED, Fulfilment.ACTIVATING, Fulfilment.FAILED,
        ],
    )
    return [o for o in candidates if attempts(o) < MAX_ATTEMPTS]
