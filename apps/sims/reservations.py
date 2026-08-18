"""SIM reservation and assignment.

Port of the plugin's reservation logic (`reserve_sims_for_cart`,
`release_*`, `assign_sims_to_order`). Selection rules, matching the plugin:

* inventory must be IN_STOCK
* provisioning_status must be "available" (case/space-insensitive)
* the SIM must have an MSISDN (empty-MSISDN SIMs are never sold)
* type_of_sim must match the requested delivery type (esim / psim)

All state changes run inside a transaction with row locking so two
concurrent checkouts can't grab the same SIM.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Sim, SimReservation

logger = logging.getLogger("apps.sims")

# How long a cart hold lasts before cleanup releases it (plugin default: 5 min).
RESERVATION_TIMEOUT = timedelta(minutes=15)


class InsufficientInventory(Exception):
    """Raised when fewer available SIMs exist than were requested."""

    def __init__(self, sim_type, requested, found):
        self.sim_type = sim_type
        self.requested = requested
        self.found = found
        super().__init__(
            f"Insufficient {sim_type} inventory: needed {requested}, found {found}."
        )


def _available_queryset(sim_type_key: str):
    """SIMs that can be sold for the given delivery type."""
    has_msisdn = ~Q(msisdn="") & Q(msisdn__isnull=False)
    qs = (
        Sim.objects.filter(inventory=Sim.Inventory.IN_STOCK)
        .filter(has_msisdn)
        .annotate()  # placeholder to keep chaining readable
    )
    # provisioning_status == "available", tolerant of case/whitespace.
    qs = qs.filter(provisioning_status__iexact="available")

    # Transatel park export uses type_of_sim = "eUICC" (embedded/eSIM) vs
    # "UICC" (physical/pSIM). "UICC" is a substring of "eUICC", so eSIM matches
    # on "euicc" and pSIM is everything that is NOT eUICC.
    if sim_type_key == "esim":
        qs = qs.filter(Q(type_of_sim__icontains="euicc") | Q(type_of_sim__icontains="esim"))
    else:
        # Physical (UICC): anything that isn't an embedded eUICC / eSIM.
        qs = qs.exclude(type_of_sim__icontains="euicc").exclude(type_of_sim__icontains="esim")
    return qs.order_by("id")


def count_available(sim_type_key: str) -> int:
    return _available_queryset(sim_type_key).count()


def latest_available_sim() -> Sim | None:
    """The most recently imported sellable SIM (any delivery type).

    "Latest" = highest `imported_at` (falling back to `id` as a tiebreaker
    for rows imported in the same batch/second). Used by the
    availability-check endpoint that hands the storefront a single ICCID.
    Returns None when nothing is available.
    """
    qs = (
        Sim.objects.filter(inventory=Sim.Inventory.IN_STOCK)
        .filter(~Q(msisdn="") & Q(msisdn__isnull=False))
        .filter(provisioning_status__iexact="available")
    )
    return qs.order_by("-imported_at", "-id").first()


def latest_available_sims(sim_type_key: str, count: int) -> list["Sim"]:
    """The `count` most recently imported sellable SIMs of one delivery type.

    "Latest" = highest `imported_at` (id as a tiebreaker). Type matching is the
    same as `_available_queryset` (esim → eUICC, psim → UICC). Read-only — this
    does NOT reserve anything, so two callers can be handed the same SIM; the
    caller must still reserve/assign before checkout.

    Returns a list that may be shorter than `count` when stock is low, or empty
    when `count <= 0`.
    """
    if count <= 0:
        return []
    sim_type_key = "esim" if sim_type_key == "esim" else "psim"
    return list(
        _available_queryset(sim_type_key).order_by("-imported_at", "-id")[:count]
    )


@transaction.atomic
def reserve_sims(sim_type_key, quantity, cart_key, session_id):
    """Place a cart hold on `quantity` SIMs. Idempotent per cart_key.

    Returns the list of reserved Sim ids. Raises InsufficientInventory.
    """
    sim_type_key = "esim" if sim_type_key == "esim" else "psim"
    now = timezone.now()

    # Re-use an existing, unexpired hold for this cart line.
    existing = list(
        SimReservation.objects.select_for_update()
        .filter(cart_key=cart_key, expires_at__gt=now)
        .values_list("sim_id", flat=True)
    )
    if len(existing) >= quantity:
        return existing[:quantity]

    needed = quantity - len(existing)
    candidates = list(
        _available_queryset(sim_type_key)
        .exclude(id__in=existing)
        .select_for_update(skip_locked=True)[:needed]
    )
    if len(candidates) < needed:
        raise InsufficientInventory(sim_type_key, quantity, len(existing) + len(candidates))

    expires_at = now + RESERVATION_TIMEOUT
    reserved_ids = list(existing)
    for sim in candidates:
        sim.inventory = Sim.Inventory.PENDING
        sim.save(update_fields=["inventory"])
        SimReservation.objects.create(
            sim=sim, cart_key=cart_key, session_id=session_id, expires_at=expires_at
        )
        reserved_ids.append(sim.id)

    logger.info(
        "Reserved %s %s SIM(s) for cart %s", len(candidates), sim_type_key, cart_key
    )
    return reserved_ids


@transaction.atomic
def release_cart(cart_key):
    """Release all holds for a cart line, returning SIMs to stock."""
    holds = SimReservation.objects.select_for_update().filter(cart_key=cart_key)
    sim_ids = list(holds.values_list("sim_id", flat=True))
    if sim_ids:
        Sim.objects.filter(id__in=sim_ids, inventory=Sim.Inventory.PENDING).update(
            inventory=Sim.Inventory.IN_STOCK
        )
    holds.delete()
    return sim_ids


@transaction.atomic
def release_expired():
    """Release every hold past its TTL. Used by the cleanup command."""
    now = timezone.now()
    expired = SimReservation.objects.select_for_update().filter(expires_at__lte=now)
    sim_ids = list(expired.values_list("sim_id", flat=True))
    if sim_ids:
        Sim.objects.filter(id__in=sim_ids, inventory=Sim.Inventory.PENDING).update(
            inventory=Sim.Inventory.IN_STOCK
        )
    count = expired.count()
    expired.delete()
    return {"released": count, "sim_ids": sim_ids}


@transaction.atomic
def assign_to_order(sim_ids, order_reference):
    """Promote PENDING holds to RESERVED and stamp the order reference.

    Called once payment succeeds and the order is being created.
    """
    sims = list(
        Sim.objects.select_for_update().filter(id__in=sim_ids)
    )
    for sim in sims:
        sim.inventory = Sim.Inventory.RESERVED
        sim.order_reference = order_reference
        sim.save(update_fields=["inventory", "order_reference"])
    # The cart holds are no longer needed once the order owns the SIMs.
    SimReservation.objects.filter(sim_id__in=sim_ids).delete()
    return sims


@transaction.atomic
def mark_activated(sim, transaction_id=""):
    sim.inventory = Sim.Inventory.ACTIVATED
    sim.activated_at = timezone.now()
    sim.activation_transaction_id = transaction_id or ""
    sim.save(
        update_fields=["inventory", "activated_at", "activation_transaction_id"]
    )
    return sim

@transaction.atomic
def return_to_stock(sims):
    """Release SIMs back to sellable INSTOCK and clear the order reference.

    Used when an order's activation fails so the SIM isn't stranded in
    RESERVED (which would leak sellable inventory on every failed attempt).
    Accepts Sim instances or ids. Skips rows already ACTIVATED, so a
    partially-successful order can't un-sell a live SIM.
    """
    ids = [getattr(s, "id", s) for s in sims]
    if not ids:
        return []
    Sim.objects.filter(id__in=ids).exclude(
        inventory=Sim.Inventory.ACTIVATED
    ).update(inventory=Sim.Inventory.IN_STOCK, order_reference="")
    return ids