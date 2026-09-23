"""Transatel API client.

Ported from the working `lib/transatel.ts` in the Next.js repo, so the auth
scheme, base URL and endpoint paths are the ones already proven against the
live API — not invented.

Auth is OAuth2 client_credentials:
    POST {BASE}/authentication/api/token
    Authorization: Basic base64(CLIENT_ID:CLIENT_SECRET)
    body: grant_type=client_credentials

The token is cached in Django's cache rather than a module global, so it is
shared across gunicorn workers instead of every worker fetching its own.
"""

import base64
import logging
import re
import threading

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

TOKEN_CACHE_KEY = "transatel:access_token"
TIMEOUT = getattr(settings, "TRANSATEL_TIMEOUT", 30)

# Transatel's own rule: SIM serial (ICCID) must be 13–20 digits.
SERIAL_RE = re.compile(r"^\d{13,20}$")

_token_lock = threading.Lock()


class TransatelError(Exception):
    """Raised when Transatel refuses or fails a request."""

    def __init__(self, message, status=502, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def valid_serial(serial):
    return bool(serial and SERIAL_RE.match(str(serial).strip()))


class TransatelClient:

    def __init__(self):
        self.base = (getattr(settings, "TRANSATEL_BASE", "") or "https://api.transatel.com").rstrip("/")
        self.client_id = getattr(settings, "TRANSATEL_CLIENT_ID", "") or ""
        self.client_secret = getattr(settings, "TRANSATEL_CLIENT_SECRET", "") or ""

    # ── auth ───────────────────────────────────────────────────────────────

    def _basic_auth(self):
        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def get_token(self, force_refresh=False):
        if not force_refresh:
            cached = cache.get(TOKEN_CACHE_KEY)
            if cached:
                return cached

        if not self.client_id or not self.client_secret:
            raise TransatelError("Transatel credentials are not configured.", 500)

        # Lock so a burst of concurrent requests fetches one token, not fifty.
        with _token_lock:
            if not force_refresh:
                cached = cache.get(TOKEN_CACHE_KEY)
                if cached:
                    return cached

            try:
                response = requests.post(
                    f"{self.base}/authentication/api/token",
                    headers={
                        "Authorization": self._basic_auth(),
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data="grant_type=client_credentials",
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                raise TransatelError(f"Could not reach Transatel: {exc}")

            if not response.ok:
                # Never log the response body here — it can echo credentials.
                logger.error("Transatel token request failed with HTTP %s", response.status_code)  # nosemgrep: python-logger-credential-disclosure -- false positive, only logs status code
                raise TransatelError("Failed to obtain Transatel token.", response.status_code)

            data = response.json()
            token = data.get("access_token")
            if not token:
                raise TransatelError("Token response missing access_token.", 502, data)

            # Refresh 60s early, same as the TS client.
            expires_in = int(data.get("expires_in", 3600))
            cache.set(TOKEN_CACHE_KEY, token, max(60, expires_in - 60))
            return token

    # ── plumbing ───────────────────────────────────────────────────────────

    def _request(self, method, path, **kwargs):
        url = f"{self.base}{path}"

        def call(token):
            return requests.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=TIMEOUT,
                **kwargs,
            )

        try:
            response = call(self.get_token())
            if response.status_code == 401:
                # Token went stale mid-flight. Refresh once and retry.
                response = call(self.get_token(force_refresh=True))
        except requests.Timeout:
            # A timeout is NOT a failure. For a provisioning call the action may
            # have completed. Callers must treat this as unknown.
            raise TransatelError("Transatel timed out. Result is unknown.", 504)
        except requests.RequestException as exc:
            raise TransatelError(f"Could not reach Transatel: {exc}")

        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:500]}

        if not response.ok:
            # Transatel returns 400 (not 404) for an unknown SIM, with a body
            # like {"detail": "Invalid SIM serial / SIM card doesn't belong to
            # customer"}. Pass the real status and body up.
            detail = (
                payload.get("detail")
                or payload.get("message")
                or f"Transatel request failed (HTTP {response.status_code})."
            ) if isinstance(payload, dict) else "Transatel request failed."
            raise TransatelError(detail, response.status_code, payload)

        return payload

    # ── endpoints ──────────────────────────────────────────────────────────

    def get_subscriber(self, sim_serial):
        """Subscriber details by SIM serial (ICCID).

        This is what replaces the `wp_transatel_sim_details` table lookup —
        status comes from Transatel live rather than a stale local copy.
        """
        if not valid_serial(sim_serial):
            raise TransatelError("SIM serial must be 13–20 digits.", 400)
        return self._request(
            "GET",
            f"/connectivity-management/subscribers/api/subscribers/sim-serial/{sim_serial}",
        )

    def get_esim(self, sim_serial, history=False):
        if not valid_serial(sim_serial):
            raise TransatelError("SIM serial must be 13–20 digits.", 400)
        flag = "true" if history else "false"
        return self._request(
            "GET",
            f"/sim-management/sims/api/esims/sim-serial/{sim_serial}?history={flag}",
        )

    def reactivate(self, sim_serial, rate_plan):
        """Reactivate a suspended SIM.

        Endpoint and payload taken verbatim from the WordPress module:
            POST /connectivity-management/subscribers/api/subscribers/
                 sim-serial/{serial}/reactivate
            {"ratePlan": "..."}
        """
        if not valid_serial(sim_serial):
            raise TransatelError("SIM serial must be 13–20 digits.", 400)
        if not rate_plan:
            raise TransatelError("A rate plan is required to reactivate.", 400)

        return self._request(
            "POST",
            f"/connectivity-management/subscribers/api/subscribers/sim-serial/{sim_serial}/reactivate",
            json={"ratePlan": rate_plan},
        )


def extract_status(subscriber_payload):
    """Pull the SIM status out of a subscriber response.

    The WordPress code read `simStatus` from its local table. The live payload
    key is not documented in anything I was given, so several likely spellings
    are tried. VERIFY THIS against a real response before going live — if the
    key is different, every recharge will be blocked as 'Unknown'.
    """
    if not isinstance(subscriber_payload, dict):
        return "Unknown"

    for key in ("simStatus", "status", "subscriberStatus", "sim_status"):
        value = subscriber_payload.get(key)
        if value:
            return str(value)

    for container in ("data", "subscriber", "sim"):
        nested = subscriber_payload.get(container)
        if isinstance(nested, dict):
            found = extract_status(nested)
            if found != "Unknown":
                return found

    return "Unknown"
