"""Swap SIM stock: reserve, release, quarantine (CTO Addendum v2.1 B4).

Reserving moves a case from CASE_ACCEPTED to STOCK_RESERVED. A SIM can be
reserved only if it is genuine Swap stock for this tenant and target type:

* a `SwapSimStock` row in AVAILABLE status, same tenant;
* the `Sim` is marked `inventory_purpose="swap"`, still IN_STOCK, has no
  pre-assigned MSISDN and backs no live line (so no Sales SIM, foreign-tenant
  SIM, assigned SIM or unverified record can enter STOCK_RESERVED);
* its format matches the case's target type;
* Network Ops records the supplier check that confirmed it is an unassigned
  Swap SIM in our pool (Phase 1, v2.1 A2 step 2).

If no eligible stock exists the caller gets NO_SWAP_STOCK, so the customer is
told about the limitation instead of receiving an impossible quote.

When a case ends, its reserved SIM goes back to AVAILABLE only if it never
left our hands (no supplier swap, never dispatched). Otherwise it is
QUARANTINED for a Network Ops check.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.add_line.models import Line
from apps.sims.models import Sim

from ..models import SimTypeChangeCase, SimTypeChangeEvent, StatusSource, SwapSimStock
from . import crypto
from .transitions import TransitionError, transition

S = SimTypeChangeCase.Status


class StockError(TransitionError):
    pass


def reservation_window() -> timedelta:
    return timedelta(hours=int(getattr(settings, "SIM_CHANGE_STOCK_RESERVATION_HOURS", 48)))


def stock_problem(stock: SwapSimStock, case: SimTypeChangeCase) -> str | None:
    """Why this stock row can't be reserved for this case, or None."""
    sim = stock.sim
    if stock.status != SwapSimStock.Status.AVAILABLE:
        return "SWAP_SIM_NOT_AVAILABLE"
    if stock.tenant != case.tenant:
        return "SWAP_SIM_WRONG_TENANT"
    if sim.inventory_purpose != Sim.Purpose.SWAP:
        return "NOT_A_SWAP_SIM"
    if (sim.msisdn or "").strip():
        return "SWAP_SIM_HAS_NUMBER"
    if sim.inventory != Sim.Inventory.IN_STOCK:
        return "SWAP_SIM_NOT_IN_STOCK"
    if stock.sim_format != case.to_type or sim.sim_type_key != case.to_type:
        return "SWAP_SIM_WRONG_FORMAT"
    if Line.objects.filter(sim=sim, status__in=Line.CAPACITY_STATUSES).exists():
        return "SWAP_SIM_ASSIGNED"
    return None


def available_count(case: SimTypeChangeCase) -> int:
    return SwapSimStock.objects.filter(
        tenant=case.tenant, status=SwapSimStock.Status.AVAILABLE, sim_format=case.to_type,
        sim__inventory_purpose=Sim.Purpose.SWAP, sim__inventory=Sim.Inventory.IN_STOCK, sim__msisdn="",
    ).count()


def reserve_for_case(case: SimTypeChangeCase, *, actor, supplier_check_reference: str,
                     stock: SwapSimStock | None = None) -> SwapSimStock:
    """Reserve a Swap SIM for `case` and move it to STOCK_RESERVED (one transaction)."""
    if not supplier_check_reference.strip():
        raise StockError("SUPPLIER_CHECK_REQUIRED", "Record the supplier check that confirmed this Swap SIM.", 422)

    with transaction.atomic():
        case = SimTypeChangeCase.objects.select_for_update().get(pk=case.pk)
        if case.status != S.CASE_ACCEPTED:
            raise StockError("TRANSITION_NOT_ALLOWED", "Stock can only be reserved for an accepted request.", 409)

        if stock is not None:
            stock = SwapSimStock.objects.select_for_update().select_related("sim").get(pk=stock.pk)
            problem = stock_problem(stock, case)
            if problem:
                raise StockError(problem, "That SIM can't be used for this change.", 409)
        else:
            candidates = SwapSimStock.objects.select_for_update().select_related("sim").filter(
                tenant=case.tenant, status=SwapSimStock.Status.AVAILABLE, sim_format=case.to_type,
            ).order_by("imported_at", "id")
            stock = next((c for c in candidates if stock_problem(c, case) is None), None)
            if stock is None:
                raise StockError(
                    "NO_SWAP_STOCK", "There's no replacement SIM available for this change right now.", 409,
                )

        now = timezone.now()
        stock.status = SwapSimStock.Status.RESERVED
        stock.reserved_case = case
        stock.reservation_expires_at = now + reservation_window()
        stock.reserved_by = actor
        stock.supplier_check_reference = supplier_check_reference.strip()[:128]
        stock.save()

        sim = stock.sim
        sim.inventory = Sim.Inventory.RESERVED
        sim.order_reference = f"SIMCHG-{case.uuid.hex[:12]}"
        sim.save(update_fields=["inventory", "order_reference"])

        case.new_sim = sim
        case.save(update_fields=["new_sim", "updated_at"])
        SimTypeChangeEvent.objects.create(
            tenant=case.tenant, case=case, actor=actor, actor_role="network_ops", kind="stock_reserved",
            status_source=StatusSource.SUPPORT, version=case.version,
            detail={"sim": crypto.mask_serial(sim.iccid), "format": stock.sim_format},
        )
        transition(case, S.STOCK_RESERVED, source=StatusSource.SUPPORT, actor=actor, actor_role="network_ops")
    return stock


def release_for_case(case: SimTypeChangeCase, reason: str) -> SwapSimStock | None:
    """Called when a case ends. Must run inside the case's transaction."""
    stock = SwapSimStock.objects.select_for_update().select_related("sim").filter(
        reserved_case=case, status=SwapSimStock.Status.RESERVED,
    ).first()
    if stock is None:
        return None

    left_our_hands = case.supplier_submitted or case.dispatched_at is not None
    if left_our_hands or case.status == S.CLOSED:
        # Possibly bound at the supplier or physically with the customer.
        stock.status = SwapSimStock.Status.QUARANTINED
        stock.quarantine_reason = f"case ended ({reason}) after the SIM left stock"[:255]
        stock.save(update_fields=["status", "quarantine_reason", "updated_at"])
        return stock

    stock.status = SwapSimStock.Status.AVAILABLE
    stock.reserved_case = None
    stock.reservation_expires_at = None
    stock.save(update_fields=["status", "reserved_case", "reservation_expires_at", "updated_at"])
    sim = stock.sim
    sim.inventory = Sim.Inventory.IN_STOCK
    sim.order_reference = ""
    sim.save(update_fields=["inventory", "order_reference"])
    return stock


def quarantine(stock: SwapSimStock, *, reason: str, actor=None) -> SwapSimStock:
    """Take a stock row out of use pending a Network Ops check."""
    if not reason.strip():
        raise StockError("REASON_REQUIRED", "Say why this SIM is being quarantined.", 422)
    with transaction.atomic():
        stock = SwapSimStock.objects.select_for_update().get(pk=stock.pk)
        if stock.status == SwapSimStock.Status.ASSIGNED:
            raise StockError("SWAP_SIM_ASSIGNED", "This SIM is bound to a line and can't be quarantined here.", 409)
        stock.status = SwapSimStock.Status.QUARANTINED
        stock.quarantine_reason = reason.strip()[:255]
        stock.save(update_fields=["status", "quarantine_reason", "updated_at"])
    return stock


def flag_expired_reservations(now=None) -> int:
    """Send cases whose reservation lapsed before dispatch/swap to support.

    The SIM stays reserved: a person decides whether to continue or cancel
    (cancelling releases it). Returns how many cases were flagged.
    """
    now = now or timezone.now()
    flagged = 0
    expired = SwapSimStock.objects.filter(
        status=SwapSimStock.Status.RESERVED, reservation_expires_at__lt=now,
        reserved_case__status=S.STOCK_RESERVED, reserved_case__dispatched_at__isnull=True,
    ).select_related("reserved_case")
    for stock in expired:
        try:
            transition(stock.reserved_case, S.SUPPORT_REQUIRED, source=StatusSource.SYSTEM,
                       reason="Swap SIM reservation expired before dispatch or swap")
            flagged += 1
        except TransitionError:
            continue  # the case moved on in the meantime
    return flagged
