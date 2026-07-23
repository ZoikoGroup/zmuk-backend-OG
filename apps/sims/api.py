"""SIM ordering API.

Endpoints (see urls.py):

    GET  /api/v1/sims/availability/?sim_type=esim   → { esim: N, psim: M }
    POST /api/v1/sims/reserve/                       → place a cart hold
    POST /api/v1/sims/release/                       → release a cart hold
    POST /api/v1/sim-orders/                         → reserve + activate SIMs

The last endpoint replaces the checkout's old BT-era order route. It runs the
persist-first-then-activate flow: reserve a SIM per line, stamp the order
reference, then activate it against the Transatel API using the plan's
`transatelID` as the package code and the SIM's serial number.
"""

from __future__ import annotations

import logging
import time

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import reservations
from .models import Sim
from .serializers import (
    ReleaseSerializer,
    ReserveSerializer,
    SimOrderSerializer,
    SimSerializer,
)
from .transatel import TransatelError, get_service
from .transatel.exceptions import TransatelNotConfigured

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
    """Reserve, assign, and activate SIMs for a checkout order."""

    def post(self, request):
        s = SimOrderSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        order_reference = data.get("order_reference") or f"ZK-{int(time.time())}"
        country_code = data.get("country_code") or ""

        try:
            service = get_service()
        except TransatelNotConfigured as exc:
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

            # 3) Activate each SIM against Transatel using the plan's code.
            for sim in sims:
                serial = sim.serial_number or sim.iccid
                try:
                    resp = service.activate_or_modify(
                        serial_number=serial,
                        package_code=package_code,
                        order_reference=order_reference,
                        country_code=country_code,
                    )
                    tx_id = ""
                    if isinstance(resp, dict):
                        tx_id = str(resp.get("transactionId", ""))
                    reservations.mark_activated(sim, transaction_id=tx_id)
                    assigned.append({
                        "iccid": sim.iccid,
                        "msisdn": sim.msisdn,
                        "sim_type": sim.sim_type_key,
                        "transatelID": package_code,
                        "transaction_id": tx_id,
                    })
                except TransatelError as exc:
                    logger.error("Activation failed for %s: %s", serial, exc)
                    errors.append(f"{sim.iccid}: {exc}")

        if not assigned:
            return Response(
                {"success": False,
                 "message": errors[0] if errors else "No SIMs could be assigned.",
                 "errors": errors},
                status=status.HTTP_409_CONFLICT,
            )

        return Response({
            "success": True,
            "order_reference": order_reference,
            "assigned": assigned,
            "errors": errors,  # partial failures, if any
        })
