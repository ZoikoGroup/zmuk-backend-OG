"""
Malima views.

Two endpoints:

  POST /api/v1/malima/allocate-sims/
      Body: AllocateSimsRequestSerializer (see serializers.py)
      Reserves one SIM per cart unit. Concurrency-safe via select_for_update.

  POST /api/v1/malima-orders/
      Body: MalimaOrderCreateSerializer
      Persists the full Orange request/response and marks reservations
      as consumed (success) or released (failure).

A small helper:

  POST /api/v1/malima/release/   (optional)
      Body: { "reservation_ids": [...] }
      Manual safety valve for ops if a checkout dies between allocate and save.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import MalimaOrder, MalimaOrderLine, SimInventory, SimReservation
from .serializers import (
    AllocateSimsRequestSerializer,
    MalimaOrderCreateSerializer,
)

logger = logging.getLogger(__name__)

# How long an allocation is held before being released automatically.
# 15 minutes covers Stripe 3DS plus a slow checkout; tune per your flow.
RESERVATION_TTL = timedelta(minutes=15)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _decimal(value: Any) -> Decimal:
    """Best-effort decimal parse for totals coming from JSON."""
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _envelope(success: bool, message: str = "", **extra) -> dict:
    """Match the existing /api/v1/bqorders/ response shape."""
    return {"success": success, "message": message, **extra}


# ── 1. Allocate SIMs ─────────────────────────────────────────────────────────

class AllocateSimsView(APIView):
    """
    Reserve one SIM per cart unit, atomically.

    The hot path uses `select_for_update(skip_locked=True)` so concurrent
    checkouts don't block each other — each grabs a different row from
    the available pool.
    """

    permission_classes = [AllowAny]  # Add IsAuthenticated/TokenAuth as needed.

    def post(self, request, *args, **kwargs):
        serializer = AllocateSimsRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                _envelope(False, "Invalid request.", errors=serializer.errors),
                status=status.HTTP_400_BAD_REQUEST,
            )

        cart = serializer.validated_data["cart"]
        source = serializer.validated_data.get("source") or "zoiko-orbit-checkout"
        expires_at = timezone.now() + RESERVATION_TTL

        allocations: list[dict] = []
        created_reservation_ids: list[str] = []

        try:
            with transaction.atomic():
                for entry in cart:
                    sim_type = entry["simType"]                # already normalised
                    zone = (entry.get("roamingZone") or "").strip()

                    # Pick the next available SIM, locking the row so no other
                    # transaction picks the same one.
                    qs = (
                        SimInventory.objects
                        .select_for_update(skip_locked=True)
                        .filter(status=SimInventory.Status.AVAILABLE, sim_type=sim_type)
                        .order_by("id")
                    )
                    # If a roaming zone was requested, prefer zone-matching SIMs
                    # but fall back to unrestricted inventory if none are tagged.
                    sim = None
                    if zone:
                        sim = qs.filter(roaming_zone=zone).first()
                    if sim is None:
                        sim = qs.filter(roaming_zone="").first()
                    if sim is None:
                        # No inventory — roll back everything allocated so far.
                        raise _OutOfInventory(sim_type, zone)

                    # Flip status and create the reservation in the same txn.
                    sim.status = SimInventory.Status.RESERVED
                    sim.save(update_fields=["status", "updated_at"])

                    reservation_id = f"res-{uuid.uuid4().hex[:16]}"
                    SimReservation.objects.create(
                        reservation_id=reservation_id,
                        sim=sim,
                        cart_index=entry["cartIndex"],
                        unit_index=entry["unitIndex"],
                        plan_id=str(entry.get("planId") or entry.get("transatelID,") or ""),
                        status=SimReservation.Status.ACTIVE,
                        expires_at=expires_at,
                    )

                    allocations.append({
                        "cartIndex": entry["cartIndex"],
                        "unitIndex": entry["unitIndex"],
                        "msisdn": sim.msisdn,
                        "imsi": sim.imsi,
                        "iccid": sim.iccid,
                        "reservation_id": reservation_id,
                    })
                    created_reservation_ids.append(reservation_id)

        except _OutOfInventory as e:
            logger.warning(
                "Malima allocation rejected — out of inventory for %s/%s (source=%s)",
                e.sim_type, e.zone or "<any>", source,
            )
            return Response(
                _envelope(
                    False,
                    f"Not enough {e.sim_type} inventory"
                    + (f" for zone '{e.zone}'" if e.zone else "")
                    + ".",
                    allocations=[],
                ),
                status=status.HTTP_200_OK,  # 200 with success:false (matches Next.js proxy expectations)
            )
        except Exception as exc:                       # pragma: no cover
            logger.exception("Malima allocation failed: %s", exc)
            return Response(
                _envelope(False, "Allocation error — please retry."),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            _envelope(
                True,
                f"Allocated {len(allocations)} SIM(s).",
                allocations=allocations,
            ),
            status=status.HTTP_200_OK,
        )


class _OutOfInventory(Exception):
    def __init__(self, sim_type: str, zone: str = "") -> None:
        self.sim_type = sim_type
        self.zone = zone


# ── 2. Save Malima order ─────────────────────────────────────────────────────

class MalimaOrderView(APIView):
    """
    Persist the full Malima order capture from Next.js.

    Side-effects on top of just storing the record:
      • Marks every reservation referenced by a successful line as `consumed`
        and the underlying SIM as `activated`.
      • Marks reservations for failed lines (and any reservations that were
        allocated but not used) as `released`, returning their SIMs to the
        available pool.
    """

    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = MalimaOrderCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                _envelope(False, "Invalid request.", errors=serializer.errors),
                status=status.HTTP_400_BAD_REQUEST,
            )

        envelope = serializer.validated_data
        data = envelope["data"]
        malima = data.get("malima", {}) or {}
        results = malima.get("results") or []
        totals = data.get("totals") or {}
        billing = data.get("billingAddress") or {}
        shipping = data.get("shippingAddress") or {}
        allocations = data.get("allocations") or []

        # Build a (cartIndex, unitIndex) → reservation_id index so we can
        # cross-reference each line back to its reservation.
        alloc_index: dict[tuple[int, int], str] = {}
        for a in allocations:
            try:
                alloc_index[(int(a["cartIndex"]), int(a.get("unitIndex", 0)))] = (
                    a.get("reservation_id") or ""
                )
            except (KeyError, TypeError, ValueError):
                continue

        # Decide the overall order status from per-line outcomes.
        oks = [bool(r.get("ok")) for r in results]
        if not oks:
            overall = MalimaOrder.Status.PENDING
        elif all(oks):
            overall = MalimaOrder.Status.SUCCESS
        elif any(oks):
            overall = MalimaOrder.Status.PARTIAL
        else:
            overall = MalimaOrder.Status.FAILED

        try:
            with transaction.atomic():
                order = MalimaOrder.objects.create(
                    source=envelope.get("source") or "zoiko-orbit-checkout",
                    status=overall,
                    email=(billing.get("email") or "").strip(),
                    customer_name=(
                        f"{billing.get('firstName', '')} {billing.get('lastName', '')}"
                    ).strip(),
                    billing_address=billing,
                    shipping_address=shipping,
                    stripe_payment_intent_id=(
                        data.get("stripe_payment_intent_id")
                        or data.get("paymentIntentId")
                        or ""
                    ),
                    bequick_order_id=str(data.get("bequick_order_id") or ""),
                    subtotal=_decimal(totals.get("subtotal")),
                    shipping=_decimal(totals.get("shipping")),
                    activation_fee=_decimal(totals.get("activationFee")),
                    discount=_decimal(totals.get("discount")),
                    total=_decimal(totals.get("total")),
                    currency=(data.get("currency") or "USD")[:8],
                    raw_payload=data,
                    captured_at=envelope.get("captured_at") or timezone.now(),
                )

                consumed_res_ids: set[str] = set()
                released_res_ids: set[str] = set()

                # Persist one line per Orange result.
                for r in results:
                    cart_idx = int(r.get("cartIndex", 0))
                    unit_idx = int(r.get("unitIndex", 0))
                    res_id = alloc_index.get((cart_idx, unit_idx), "")
                    reservation = None
                    if res_id:
                        reservation = (
                            SimReservation.objects
                            .select_related("sim")
                            .filter(reservation_id=res_id)
                            .first()
                        )

                    MalimaOrderLine.objects.create(
                        order=order,
                        cart_index=cart_idx,
                        unit_index=unit_idx,
                        plan_id=str(r.get("planId") or ""),
                        sim_type=str(r.get("simType") or ""),
                        reservation=reservation,
                        msisdn=str(r.get("msisdn") or ""),
                        imsi=str(r.get("imsi") or ""),
                        iccid=str(r.get("iccid") or ""),
                        orange_order_id=str(r.get("orangeOrderId") or ""),
                        http_status=int(r.get("status") or 0),
                        ok=bool(r.get("ok")),
                        error=str(r.get("error") or "")[:512],
                        request_payload=r.get("request") or {},
                        response_payload=r.get("response") or {},
                    )

                    if res_id:
                        (consumed_res_ids if r.get("ok") else released_res_ids).add(res_id)

                # Any allocations not referenced in results (or marked failed)
                # get their SIMs returned to the pool.
                referenced = {a.get("reservation_id") for a in allocations if a.get("reservation_id")}
                orphans = referenced - consumed_res_ids - released_res_ids
                released_res_ids |= orphans

                self._mark_reservations(
                    consumed_res_ids,
                    SimReservation.Status.CONSUMED,
                    SimInventory.Status.ACTIVATED,
                )
                self._mark_reservations(
                    released_res_ids,
                    SimReservation.Status.RELEASED,
                    SimInventory.Status.AVAILABLE,
                )

        except Exception as exc:                       # pragma: no cover
            logger.exception("Malima save-order failed: %s", exc)
            return Response(
                _envelope(False, "Could not save order — please retry."),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            _envelope(
                True,
                "Order captured.",
                order_id=order.pk,
                status_=order.status,
                line_count=order.lines.count(),
            ),
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _mark_reservations(ids: set[str], res_status: str, sim_status: str) -> None:
        if not ids:
            return
        now = timezone.now()
        reservations = SimReservation.objects.select_related("sim").filter(
            reservation_id__in=ids,
            status=SimReservation.Status.ACTIVE,
        )
        for r in reservations:
            r.status = res_status
            if res_status == SimReservation.Status.CONSUMED:
                r.consumed_at = now
            else:
                r.released_at = now
            r.save(update_fields=["status", "consumed_at", "released_at"])
            if r.sim_id:
                # Don't downgrade a SIM that's already been suspended/retired
                # by ops while a reservation was open.
                if r.sim.status in (SimInventory.Status.AVAILABLE, SimInventory.Status.RESERVED):
                    r.sim.status = sim_status
                    r.sim.save(update_fields=["status", "updated_at"])


# ── 3. Manual release (optional safety valve) ───────────────────────────────

class ReleaseReservationsView(APIView):
    """
    Release one or more reservations back to the available pool.

    Useful when a checkout dies after allocate but before save (e.g. Stripe
    succeeded but the browser crashed). Call from ops tooling or a cron
    that scans for expired reservations.
    """

    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        ids = request.data.get("reservation_ids") or []
        if not isinstance(ids, list) or not ids:
            return Response(
                _envelope(False, "`reservation_ids` must be a non-empty list."),
                status=status.HTTP_400_BAD_REQUEST,
            )

        MalimaOrderView._mark_reservations(
            set(ids),
            SimReservation.Status.RELEASED,
            SimInventory.Status.AVAILABLE,
        )
        return Response(_envelope(True, f"Released {len(ids)} reservation(s)."))

class ChangePasswordAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.set_password(serializer.validated_data["password"])
        user.save()

        # Rotate the auth token so the new session stays valid after the change.
        Token.objects.filter(user=user).delete()
        token = Token.objects.create(user=user)

        return Response({
            "message": "Password updated successfully",
            "token": token.key,
        })
