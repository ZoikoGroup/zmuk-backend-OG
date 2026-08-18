"""SIM ordering API.

Endpoints (see urls.py):

    GET  /api/v1/sims/availability/?sim_type=esim   → { esim: N, psim: M }
    POST /api/v1/sims/reserve/                       → place a cart hold
    POST /api/v1/sims/release/                       → release a cart hold
    POST /api/v1/sim-orders/                         → reserve + activate SIMs

The last endpoint replaces the checkout's old BT-era order route. It runs the
persist-first-then-activate flow: reserve a SIM per line, stamp the order
reference, then run the subscriber flow against the Transatel API —
    check status by SIM serial → activate (or /modify) with the plan's
    `transatelID` as the package code → push the customer's contact info from
    the billing address to /contact-info.
"""

from __future__ import annotations

import logging
import time

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import reservations
from .esim import deliver_qr
from .psim import deliver_details
from .models import Order, Sim
from .serializers import (
    LatestAvailableSimSerializer,
    OrderDetailSerializer,
    ReleaseSerializer,
    ReserveSerializer,
    SimOrderSerializer,
)
from .transatel import TransatelError, get_service
from .transatel.exceptions import TransatelNotConfigured
from .transatel.service import build_subscriber_info

logger = logging.getLogger("apps.sims")


class AvailabilityView(APIView):
    """How many SIMs are sellable right now, per delivery type."""

    def get(self, request):
        sim_type = request.query_params.get("sim_type")
        if sim_type in ("esim", "psim"):
            return Response({sim_type: reservations.count_available(sim_type)})
        return Response({
            "esim": reservations.count_available("esim"),
            "psim": reservations.count_available("psim"),
        })


class LatestAvailableIccidView(APIView):
    """Latest available SIM info, by requested quantity per delivery type.

    GET or POST /api/v1/sim/sims/availability/latest/

    Ask for however many of each type the cart needs and get back that many of
    the most recently imported sellable SIMs of that type. Read-only — it does
    NOT reserve anything; call `ReserveView`/`SimOrderView` to actually hold or
    assign SIMs before checkout, since availability can change between requests.

    Counts may be sent as query params or a JSON body:
        ?esim=1&psim=2            or        { "esim": 1, "psim": 2 }

    Type mapping (park export `type_of_sim`):
        esim → eUICC (embedded)             psim → UICC (physical)

    Response (200):
        {
          "success": true,                       # true only if fully satisfied
          "requested":  { "esim": 1, "psim": 2 },
          "returned":   { "esim": 1, "psim": 2 },
          "shortfall":  { "esim": 0, "psim": 0 },
          "sufficient": true,
          "sims": {
            "esim": [ { "iccid", "msisdn", "serial_number",
                        "type_of_sim", "sim_type", "provisioning_status" } ],
            "psim": [ { ... }, { ... } ]
          }
        }

    With no counts supplied, falls back to the original behaviour: a single
    latest available SIM of any type —
        200 { "success": true, "iccid": ..., "msisdn": ..., ... }
        404 { "success": false, "message": "No available SIMs in stock." }
    """

    def get(self, request):
        return self._respond(self._counts_from(request.query_params))

    def post(self, request):
        return self._respond(self._counts_from(request.data))

    @staticmethod
    def _counts_from(source) -> dict:
        """Read non-negative esim/psim integers from a query-dict or JSON body."""
        def n(key):
            try:
                return max(0, int(source.get(key, 0) or 0))
            except (TypeError, ValueError):
                return 0
        return {"esim": n("esim"), "psim": n("psim")}

    def _respond(self, requested: dict):
        # No counts → legacy single-SIM lookup (any delivery type).
        if requested["esim"] == 0 and requested["psim"] == 0:
            sim = reservations.latest_available_sim()
            if sim is None:
                return Response(
                    {"success": False, "message": "No available SIMs in stock."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response({"success": True, **LatestAvailableSimSerializer(sim).data})

        sims, returned, shortfall = {}, {}, {}
        for sim_type in ("esim", "psim"):
            want = requested[sim_type]
            found = reservations.latest_available_sims(sim_type, want)
            sims[sim_type] = LatestAvailableSimSerializer(found, many=True).data
            returned[sim_type] = len(found)
            shortfall[sim_type] = max(0, want - len(found))

        sufficient = shortfall["esim"] == 0 and shortfall["psim"] == 0
        return Response({
            "success": sufficient,
            "requested": requested,
            "returned": returned,
            "shortfall": shortfall,
            "sufficient": sufficient,
            "sims": sims,
        })


class ReserveView(APIView):
    def post(self, request):
        s = ReserveSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        try:
            sim_ids = reservations.reserve_sims(
                d["simType"], d["quantity"], d["cart_key"], d["session_id"]
            )
        except reservations.InsufficientInventory as exc:
            return Response(
                {"success": False, "message": str(exc),
                 "requested": exc.requested, "available": exc.found},
                status=status.HTTP_409_CONFLICT,
            )
        return Response({"success": True, "reserved": sim_ids})


class ReleaseView(APIView):
    def post(self, request):
        s = ReleaseSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        released = reservations.release_cart(s.validated_data["cart_key"])
        return Response({"success": True, "released": released})


class SimOrderView(APIView):
    """Reserve, assign, and activate SIMs for a checkout order.

    Flow per line item (matching the plugin's checkout path):

        1. reserve `quantity` SIMs of the line's `simType` from inventory;
        2. assign them to `order_reference` (PENDING → RESERVED);
        3. for each SIM: check the subscriber status by serial, activate the
           SIM (or /modify if already active) attaching the plan by its
           `transatelID`, then push the customer's contact info to /contact-info.
    """

    def post(self, request):
        s = SimOrderSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        order_reference = data.get("order_reference") or f"ZK-{int(time.time())}"
        country_code = data.get("country_code") or ""

        # Build the contact-info block once for the whole order (billing address).
        subscriber_info = build_subscriber_info(data)

        # Recipient for fulfilment emails (eSIM QR / pSIM ICCID-IMSI-MSISDN):
        # the checkout's billing-address email, falling back to the top-level
        # order email if the billing address didn't include one.
        billing = data.get("billing") or {}
        billing_email = billing.get("email") or data.get("email") or ""

        # Save the order as soon as it's received, before touching Transatel,
        # so a durable record exists even if activation fails outright.
        # `update_or_create` makes this idempotent on retried `order_reference`s.
        order, _created = Order.objects.update_or_create(
            order_reference=order_reference,
            defaults={
                "email": data.get("email") or "",
                "billing_email": billing_email,
                "user_id": str(data.get("user_id") or ""),
                "country_code": country_code,
                "payload": request.data,
                "subscriber": subscriber_info,
                "status": Order.STATUS_RECEIVED,
            },
        )

        try:
            service = get_service()
        except TransatelNotConfigured as exc:
            order.status = Order.STATUS_FAILED
            order.errors = [str(exc)]
            order.save(update_fields=["status", "errors", "updated_at"])
            return Response(
                {"success": False, "message": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        assigned = []
        errors = []

        for item in data["items"]:
            sim_type = item["simType"]
            package_code = item["transatelID"]
            quantity = item["quantity"]
            cart_key = item.get("cartKey") or f"{order_reference}-{package_code}"

            # 1) Reserve inventory for this line.
            try:
                sim_ids = reservations.reserve_sims(
                    sim_type, quantity, cart_key, session_id=order_reference
                )
            except reservations.InsufficientInventory as exc:
                errors.append(str(exc))
                continue

            # 2) Assign the reserved SIMs to this order.
            sims = reservations.assign_to_order(sim_ids, order_reference)

            # 3) Run the subscriber flow for each SIM.
            for sim in sims:
                serial = sim.serial_number or sim.iccid
                try:
                    result = service.activate_order_sim(
                        serial_number=serial,
                        package_code=package_code,
                        order_reference=order_reference,
                        country_code=country_code,
                        subscriber_info=subscriber_info,
                    )
                    tx_id = result.get("transaction_id", "")
                    reservations.mark_activated(sim, transaction_id=tx_id)

                    # Persist the MSISDN Transatel reports back, if we didn't
                    # already have it from the park import.
                    msisdn = result.get("msisdn") or sim.msisdn
                    if msisdn and msisdn != sim.msisdn:
                        sim.msisdn = msisdn
                        sim.save(update_fields=["msisdn"])

                    contact = result.get("contact")
                    contact_status = (
                        contact.get("status") if isinstance(contact, dict) else None
                    )
                    if contact_status == "error":
                        # Activation succeeded but the contact push failed —
                        # surface it as a soft error without failing the order.
                        errors.append(
                            f"{sim.iccid}: contact-info not updated "
                            f"({contact.get('detail', 'unknown error')})"
                        )
                    elif contact_status == "pending":
                        # SIM still provisioning; contact-info deferred, not failed.
                        # Stash the contact block so `push_pending_contact` can
                        # complete it once the SIM goes Active.
                        sim.pending_contact = subscriber_info or None
                        sim.save(update_fields=["pending_contact"])
                        errors.append(
                            f"{sim.iccid}: contact-info deferred "
                            f"(SIM still provisioning — retry once active)"
                        )

                    assigned.append({
                        "iccid": sim.iccid,
                        "msisdn": msisdn,
                        "sim_type": sim.sim_type_key,
                        "transatelID": package_code,
                        "transaction_id": tx_id,
                        "prior_status": result.get("prior_status", ""),
                        "post_status": result.get("post_status", ""),
                        "already_active": result.get("already_active", False),
                        "contact_updated": contact_status == "success",
                        "contact_pending": contact_status == "pending",
                        "activation": result.get("activation"),
                    })

                    # Best-effort: try fetching the eSIM QR right now. Transatel
                    # provisioning is async, so this often isn't ready yet —
                    # that's fine, it's a no-op ("not_ready") and the scheduled
                    # `python manage.py send_esim_qr` run picks it up shortly
                    # after. This is purely an optimisation so the QR can show
                    # up immediately when Transatel happens to be fast; never
                    # let it fail the order.
                    if sim.is_esim:
                        try:
                            deliver_qr(service, sim)
                        except Exception:
                            logger.warning(
                                "Inline eSIM QR fetch failed for %s; "
                                "will retry via send_esim_qr.", sim.iccid,
                                exc_info=True,
                            )
                    else:
                        # pSIM: ICCID/IMSI/MSISDN are already on the row, so
                        # this can be sent right away — no async profile to
                        # wait on. Best-effort, same as the eSIM QR above:
                        # never let it fail the order; `send_psim_details`
                        # picks up anything missed (e.g. no billing_email
                        # yet) on its next scheduled run.
                        try:
                            deliver_details(sim)
                        except Exception:
                            logger.warning(
                                "Inline pSIM details email failed for %s; "
                                "will retry via send_psim_details.", sim.iccid,
                                exc_info=True,
                            )
                except TransatelError as exc:
                    logger.error("Activation failed for %s: %s", serial, exc)
                    # Activation failed — don't strand the SIM in RESERVED.
                    # Return it to sellable stock so a retry (or another order)
                    # can use it, and so failed attempts don't leak inventory.
                    reservations.return_to_stock([sim])
                    errors.append(f"{sim.iccid}: {exc}")

        if not assigned:
            order.status = Order.STATUS_FAILED
            order.assigned = assigned
            order.errors = errors
            order.save(update_fields=["status", "assigned", "errors", "updated_at"])
            return Response(
                {"success": False,
                 "message": errors[0] if errors else "No SIMs could be assigned.",
                 "errors": errors},
                status=status.HTTP_409_CONFLICT,
            )

        order.status = Order.STATUS_PARTIAL if errors else Order.STATUS_COMPLETED
        order.assigned = assigned
        order.errors = errors
        order.save(update_fields=["status", "assigned", "errors", "updated_at"])

        return Response({
            "success": True,
            "order_reference": order_reference,
            "assigned": assigned,
            "errors": errors,  # partial failures, if any
        })


class OrderDetailView(APIView):
    """GET /api/v1/sims/orders/<order_reference>/  → one order + its SIMs.

    Feeds the storefront's "order section" (order confirmation / order
    history page): per SIM line it includes the eSIM QR image URL (saved to
    media storage by `apps.sims.esim.deliver_qr`) alongside the LPA string and
    activation code — populated for eSIMs only. pSIM lines carry the same
    shape with those fields as `null`, since no QR is ever generated for a
    physical SIM.
    """

    def get(self, request, order_reference):
        order = Order.objects.filter(order_reference=order_reference).first()
        if not order:
            return Response(
                {"success": False, "message": "Order not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        data = OrderDetailSerializer(order, context={"request": request}).data
        return Response({"success": True, "order": data})


class OrdersByUserView(APIView):
    """POST /api/v1/sims/orders/by-user/  { email }  → that user's orders.

    Same shape as `OrderDetailView`, one entry per order, most recent first.
    """

    def post(self, request):
        email = (request.data.get("email") or request.data.get("logged_user") or "").strip()
        if not email:
            return Response(
                {"success": False, "message": "email is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        orders = Order.objects.filter(email__iexact=email)
        data = OrderDetailSerializer(orders, many=True, context={"request": request}).data
        return Response({"success": True, "orders": data})