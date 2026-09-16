"""Live Transatel API lookup views.

These views call the REAL Transatel API directly — no local DB involved.
They use the existing ``apps.sims.transatel.client.APIClient`` which handles
OAuth token management (auto-refresh on 401) using your credentials.

Mounted at:  /api/v1/transatel/...

Scopes exercised:
    SEARCH-SIM_SIM_READ / SEARCH_SIM_READ  → search SIMs
    CONNECTIVITY_MANAGEMENT_READ            → get subscriber
    CONNECTIVITY_MANAGEMENT_WRITE           → reactivate / suspend
    SIMS_ESIM_READ                          → eSIM details
    NETWORK-USAGE_CDR_READ                  → CDR / usage
    LINE_CONNECTIVITY_REFRESH_WRITE         → refresh connectivity
    USER_READ                               → current user / account info
    NETWORK-SMS_SMS_READ                    → read SMS
    NETWORK-SMS_SMS_WRITE                   → send SMS
    WEBHOOKS_WRITE                          → manage webhooks
"""

from __future__ import annotations

import logging

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.sims.models import Sim
from apps.sims.transatel.client import APIClient
from apps.sims.transatel.exceptions import TransatelAPIError

logger = logging.getLogger("apps.sims.transatel")


def _get_client() -> APIClient:
    return APIClient()


def _error(msg, code=500, detail=None):
    payload = {"success": False, "message": msg}
    if detail:
        payload["detail"] = detail
    return Response(payload, status=code)


def _api_call(method, endpoint, params=None, data=None):
    """Run a call and return (success_bool, response_dict, http_status)."""
    client = _get_client()
    try:
        if method == "GET":
            result = client.get(endpoint, params=params)
        elif method == "POST":
            result = client.post(endpoint, data=data)
        elif method == "PUT":
            result = client.put(endpoint, data=data)
        elif method == "PATCH":
            result = client.patch(endpoint, data=data)
        elif method == "DELETE":
            result = client.delete(endpoint)
        else:
            return False, {"error": f"Unknown method {method}"}, 400

        return result.get("success", False), result.get("data", result), result.get("status_code", 200)
    except TransatelAPIError as exc:
        logger.exception("Transatel API error: %s", exc)
        return False, {"error": str(exc)}, 502


# ═════════════════════════════════════════════════════════════════════════════
# PHONE → ICCID RESOLVER (local DB, then calls Transatel with the ICCID)
# ═════════════════════════════════════════════════════════════════════════════

class LiveLookupByPhoneView(APIView):
    """GET /api/v1/transatel/lookup-phone/<msisdn>/

    The one everyone wants: enter a phone number, get the REAL Transatel data.

    How it works:
      1. Finds the ICCID from your local sims_sim table (phone → ICCID mapping)
      2. Calls Transatel GET /subscribers/sim-serial/{ICCID} with the real ICCID
      3. Returns the LIVE subscriber data straight from Transatel

    This is NOT a local DB lookup — step 1 is local, but step 2 is a real
    network call to api.transatel.com.
    """
    permission_classes = [AllowAny]  # TODO: restrict to admin

    def get(self, request, msisdn):
        msisdn = msisdn.strip()

        # Build all format variants just like the recharge validate-phone does
        digits = msisdn.lstrip("+")
        variants = {msisdn, digits}
        if digits.startswith("44"):
            local = digits[2:]
            variants.update({f"+44{local}", f"44{local}", f"0{local}", local})
        elif digits.startswith("0"):
            bare = digits[1:]
            variants.update({f"+44{bare}", f"44{bare}", digits, bare})
        else:
            variants.update({f"+44{digits}", f"44{digits}", f"0{digits}"})

        sim = Sim.objects.filter(msisdn__in=variants).first()
        if not sim:
            return _error(
                f"Phone number {msisdn} not found in local SIM inventory. "
                f"Import your Transatel SIM park export first.",
                code=404,
            )

        iccid = sim.serial_number or sim.iccid
        if not iccid:
            return _error(
                f"SIM found for {msisdn} but has no ICCID/serial stored locally.",
                code=422,
            )

        # Now call the REAL Transatel API
        endpoint = f"/connectivity-management/subscribers/api/subscribers/sim-serial/{iccid}"
        ok, data, api_status = _api_call("GET", endpoint)

        return Response({
            "success": ok,
            "source": "transatel_live",
            "local_sim": {
                "msisdn": sim.msisdn,
                "serial_number": sim.serial_number,
                "iccid": sim.iccid,
                "local_status": sim.provisioning_status,
            },
            "transatel_response": data,
            "transatel_http_status": api_status,
        }, status=200 if ok else api_status)


# ═════════════════════════════════════════════════════════════════════════════
# SCOPE: CONNECTIVITY_MANAGEMENT_READ — Subscriber lookup by ICCID
# ═════════════════════════════════════════════════════════════════════════════

class SubscriberBySerialView(APIView):
    """GET /api/v1/transatel/subscriber/<iccid>/

    Calls: GET /connectivity-management/subscribers/api/subscribers/sim-serial/{iccid}

    Returns the LIVE subscriber record: connectivity status, MSISDN,
    active plan, data usage, account metadata.
    """
    permission_classes = [AllowAny]  # TODO: restrict to admin

    def get(self, request, iccid):
        endpoint = f"/connectivity-management/subscribers/api/subscribers/sim-serial/{iccid}"
        ok, data, api_status = _api_call("GET", endpoint)
        return Response({"success": ok, "data": data}, status=200 if ok else api_status)


# ═════════════════════════════════════════════════════════════════════════════
# SCOPE: SIMS_ESIM_READ — eSIM details by ICCID
# ═════════════════════════════════════════════════════════════════════════════

class EsimBySerialView(APIView):
    """GET /api/v1/transatel/esim/<iccid>/?history=true

    Calls: GET /sim-management/sims/api/esims/sim-serial/{iccid}?history=true

    Returns eSIM profile details and optionally the full status-change history.
    """
    permission_classes = [AllowAny]  # TODO: restrict to admin

    def get(self, request, iccid):
        history = request.query_params.get("history", "true").lower() == "true"
        endpoint = f"/sim-management/sims/api/esims/sim-serial/{iccid}"
        params = {"history": "true"} if history else None
        ok, data, api_status = _api_call("GET", endpoint, params=params)
        return Response({"success": ok, "data": data}, status=200 if ok else api_status)


# ═════════════════════════════════════════════════════════════════════════════
# SCOPE: SEARCH-SIM_SIM_READ / SEARCH_SIM_READ — Search SIMs
# ═════════════════════════════════════════════════════════════════════════════

class SearchSimsView(APIView):
    """GET /api/v1/transatel/search-sims/?status=available&limit=10

    Calls: GET /sim-management/sims/api/sims (or similar search endpoint)

    Searches the SIM inventory on Transatel's side. Pass query params through.
    """
    permission_classes = [AllowAny]  # TODO: restrict to admin

    def get(self, request):
        endpoint = "/sim-management/sims/api/sims"
        params = dict(request.query_params)
        # Flatten single-value lists from QueryDict
        params = {k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in params.items()}
        ok, data, api_status = _api_call("GET", endpoint, params=params)
        return Response({"success": ok, "data": data}, status=200 if ok else api_status)


# ═════════════════════════════════════════════════════════════════════════════
# SCOPE: CONNECTIVITY_MANAGEMENT_WRITE — Reactivate SIM
# ═════════════════════════════════════════════════════════════════════════════

class ReactivateSimView(APIView):
    """POST /api/v1/transatel/reactivate/<iccid>/

    Calls: POST /connectivity-management/subscribers/api/subscribers/sim-serial/{iccid}/reactivate

    ⚠️ THIS ACTUALLY CHANGES THE SIM ON THE LIVE NETWORK.
    """
    permission_classes = [AllowAny]  # TODO: MUST restrict to admin before production

    def post(self, request, iccid):
        rate_plan = request.data.get(
            "ratePlan",
            getattr(settings, "TRANSATEL", {}).get("RATE_PLAN", "MVNA Wholesale PAYM 7"),
        )
        endpoint = f"/connectivity-management/subscribers/api/subscribers/sim-serial/{iccid}/reactivate"
        ok, data, api_status = _api_call("POST", endpoint, data={"ratePlan": rate_plan})
        return Response({"success": ok, "data": data}, status=200 if ok else api_status)


# ═════════════════════════════════════════════════════════════════════════════
# SCOPE: CONNECTIVITY_MANAGEMENT_WRITE — Suspend SIM
# ═════════════════════════════════════════════════════════════════════════════

class SuspendSimView(APIView):
    """POST /api/v1/transatel/suspend/<iccid>/

    Calls: POST /connectivity-management/subscribers/api/subscribers/sim-serial/{iccid}/suspend

    ⚠️ THIS ACTUALLY SUSPENDS THE SIM ON THE LIVE NETWORK.
    """
    permission_classes = [AllowAny]  # TODO: MUST restrict to admin before production

    def post(self, request, iccid):
        endpoint = f"/connectivity-management/subscribers/api/subscribers/sim-serial/{iccid}/suspend"
        ok, data, api_status = _api_call("POST", endpoint, data=request.data or {})
        return Response({"success": ok, "data": data}, status=200 if ok else api_status)


# ═════════════════════════════════════════════════════════════════════════════
# SCOPE: LINE_CONNECTIVITY_REFRESH_WRITE — Refresh connectivity
# ═════════════════════════════════════════════════════════════════════════════

class RefreshConnectivityView(APIView):
    """POST /api/v1/transatel/refresh-connectivity/<iccid>/

    Calls: POST /connectivity-management/subscribers/api/subscribers/sim-serial/{iccid}/refresh
    """
    permission_classes = [AllowAny]  # TODO: restrict to admin

    def post(self, request, iccid):
        endpoint = f"/connectivity-management/subscribers/api/subscribers/sim-serial/{iccid}/refresh"
        ok, data, api_status = _api_call("POST", endpoint, data=request.data or {})
        return Response({"success": ok, "data": data}, status=200 if ok else api_status)


# ═════════════════════════════════════════════════════════════════════════════
# SCOPE: NETWORK-USAGE_CDR_READ — CDR / usage data
# ═════════════════════════════════════════════════════════════════════════════

class UsageCdrView(APIView):
    """GET /api/v1/transatel/usage/<iccid>/?from=2026-01-01&to=2026-09-16

    Calls: GET /network-usage/cdrs/api/subscribers/sim-serial/{iccid}/cdrs

    Returns call detail records / data usage for the SIM.
    """
    permission_classes = [AllowAny]  # TODO: restrict to admin

    def get(self, request, iccid):
        endpoint = f"/network-usage/cdrs/api/subscribers/sim-serial/{iccid}/cdrs"
        params = dict(request.query_params)
        params = {k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in params.items()}
        ok, data, api_status = _api_call("GET", endpoint, params=params)
        return Response({"success": ok, "data": data}, status=200 if ok else api_status)


# ═════════════════════════════════════════════════════════════════════════════
# SCOPE: NETWORK-SMS_SMS_READ — Read SMS
# ═════════════════════════════════════════════════════════════════════════════

class ReadSmsView(APIView):
    """GET /api/v1/transatel/sms/<iccid>/

    Calls: GET /network-sms/sms/api/subscribers/sim-serial/{iccid}/sms
    """
    permission_classes = [AllowAny]  # TODO: restrict to admin

    def get(self, request, iccid):
        endpoint = f"/network-sms/sms/api/subscribers/sim-serial/{iccid}/sms"
        params = dict(request.query_params)
        params = {k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in params.items()}
        ok, data, api_status = _api_call("GET", endpoint, params=params)
        return Response({"success": ok, "data": data}, status=200 if ok else api_status)


# ═════════════════════════════════════════════════════════════════════════════
# SCOPE: NETWORK-SMS_SMS_WRITE — Send SMS
# ═════════════════════════════════════════════════════════════════════════════

class SendSmsView(APIView):
    """POST /api/v1/transatel/sms/<iccid>/send/

    Calls: POST /network-sms/sms/api/subscribers/sim-serial/{iccid}/sms
    Body: { "to": "+44...", "message": "Hello" }
    """
    permission_classes = [AllowAny]  # TODO: MUST restrict to admin

    def post(self, request, iccid):
        endpoint = f"/network-sms/sms/api/subscribers/sim-serial/{iccid}/sms"
        ok, data, api_status = _api_call("POST", endpoint, data=request.data)
        return Response({"success": ok, "data": data}, status=200 if ok else api_status)


# ═════════════════════════════════════════════════════════════════════════════
# SCOPE: USER_READ — Current account / user info
# ═════════════════════════════════════════════════════════════════════════════

class AccountInfoView(APIView):
    """GET /api/v1/transatel/account/

    Calls: GET /user-management/users/api/users/me (or similar)

    Returns info about the authenticated API user (zoiko.mobile).
    """
    permission_classes = [AllowAny]  # TODO: restrict to admin

    def get(self, request):
        endpoint = "/user-management/users/api/users/me"
        ok, data, api_status = _api_call("GET", endpoint)
        return Response({"success": ok, "data": data}, status=200 if ok else api_status)


# ═════════════════════════════════════════════════════════════════════════════
# SCOPE: WEBHOOKS_WRITE — Manage webhooks
# ═════════════════════════════════════════════════════════════════════════════

class ListWebhooksView(APIView):
    """GET /api/v1/transatel/webhooks/

    Calls: GET /webhooks/api/webhooks
    """
    permission_classes = [AllowAny]  # TODO: restrict to admin

    def get(self, request):
        endpoint = "/webhooks/api/webhooks"
        ok, data, api_status = _api_call("GET", endpoint)
        return Response({"success": ok, "data": data}, status=200 if ok else api_status)


class CreateWebhookView(APIView):
    """POST /api/v1/transatel/webhooks/

    Calls: POST /webhooks/api/webhooks
    Body: { "url": "https://...", "events": ["sim.status.changed"] }
    """
    permission_classes = [AllowAny]  # TODO: MUST restrict to admin

    def post(self, request):
        endpoint = "/webhooks/api/webhooks"
        ok, data, api_status = _api_call("POST", endpoint, data=request.data)
        return Response({"success": ok, "data": data}, status=201 if ok else api_status)