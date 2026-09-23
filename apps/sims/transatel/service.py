"""High-level subscriber operations.

Port of the activation logic in the plugin's WooCommerce integration
(``get_subscriber_by_serial`` / ``activate_subscriber`` /
``modify_subscriber_add_package`` / ``updateSubscriberContactN``). The order
flow needs four Transatel calls:

Endpoints (relative to the API base URL):

    GET  /connectivity-management/subscribers/api/subscribers/sim-serial/{serial}
    POST /connectivity-management/subscribers/api/subscribers/sim-serial/{serial}/activate
    POST /connectivity-management/subscribers/api/subscribers/sim-serial/{serial}/modify
    PUT  /connectivity-management/subscribers/api/subscribers/sim-serial/{serial}/contact-info

``package_code`` is the plan's Transatel product code (e.g. ``TSL_UK_DATA_10GB``)
and is sent as an activation *option* — matching the plugin, which passes the
product code as ``options[].name`` with ``value: "on"``.

Order-flow entry point: ``activate_order_sim`` runs the whole subscriber flow
for one SIM the way the plugin does:

    1. read the subscriber's current status by SIM serial (Available / Active);
    2. Available  -> /activate  (adds the plan package);
       Active     -> /modify    (just adds the plan package);
    3. push the customer's contact info from the billing address to /contact-info.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

from .client import APIClient
from .config import TransatelConfig, get_config
from .exceptions import TransatelAPIError

logger = logging.getLogger("apps.sims.transatel")

_SUBSCRIBER_BASE = "/connectivity-management/subscribers/api/subscribers/sim-serial/"

# eSIM (LPA/QR) profile lookup — GET returns the SM-DP+ address, matching id and
# a ready-to-scan QR (LPA string + base64 PNG data URL).
_ESIM_BASE = "/sim-management/sims/api/esims/sim-serial/"

# Statuses (case-insensitive) that mean the SIM is already live, so we add the
# package via /modify instead of /activate.
_ACTIVE_STATUSES = {"active", "activated", "suspended"}

# Transatel's `subscriberCountryOfResidence` expects an ISO 3166-1 alpha-2 code.
# The storefront sends the informal "UK" (and the region label "United Kingdom
# (UK)"), so we map those to the valid "GB" here — the checkout can keep showing
# "United Kingdom (UK)" unchanged. Anything already alpha-2 passes through.
_COUNTRY_ALPHA2 = {
    "UK": "GB",
    "UNITED KINGDOM": "GB",
    "UNITED KINGDOM (UK)": "GB",
    "GREAT BRITAIN": "GB",
}


def _iso_alpha2(value: str, default: str = "GB") -> str:
    """Normalise a country value to an ISO 3166-1 alpha-2 code.

    "UK"/"United Kingdom (UK)" → "GB"; a 2-letter code is upper-cased and kept;
    blank falls back to `default`. Unknown non-2-letter values are returned
    upper-cased and trimmed so a genuinely valid code we haven't mapped still
    goes through.
    """
    raw = str(value or "").strip()
    if not raw:
        return default
    key = raw.upper()
    if key in _COUNTRY_ALPHA2:
        return _COUNTRY_ALPHA2[key]
    if len(key) == 2:
        return key
    return key


class TransatelService:
    def __init__(self, client: APIClient | None = None,
                 config: TransatelConfig | None = None):
        self.config = config or get_config()
        self.client = client or APIClient(config=self.config)

    # ── Lookup ────────────────────────────────────────────────────────────

    def get_subscriber_by_serial(self, serial_number: str) -> dict:
        """Return subscriber info for a SIM serial (status, msisdn, etc.)."""
        endpoint = _SUBSCRIBER_BASE + quote(str(serial_number), safe="")
        result = self.client.get(endpoint)
        return result["data"]

    def wait_until_provisioned(self, serial_number: str) -> str:
        """Poll subscriber status until the SIM leaves 'AVAILABLE'.

        Transatel activation is asynchronous: /activate returns a transactionId
        immediately, but the SIM stays AVAILABLE for a few seconds while the
        transaction is processed. /contact-info (and anything else that mutates
        the subscriber) is rejected while the SIM is AVAILABLE, so we wait for
        the status to move before doing follow-up calls.

        Bounded and best-effort — returns the last status seen (which may still
        be "Available" if provisioning is slower than the poll window). Poll
        parameters are read from config if present, else sane defaults:
            activation_poll_attempts (default 5), activation_poll_delay (1.5s).
        """
        attempts = int(getattr(self.config, "activation_poll_attempts", 5) or 5)
        delay = float(getattr(self.config, "activation_poll_delay", 1.5) or 1.5)
        status = ""
        for i in range(max(1, attempts)):
            try:
                current = self.get_subscriber_by_serial(serial_number)
                status = str((current or {}).get("status", "") or "").strip()
            except TransatelAPIError:
                status = status or ""
            if status and status.lower() != "available":
                logger.info(
                    "SIM %s left AVAILABLE after %ss (status=%s)",
                    serial_number, round((i) * delay, 1), status,
                )
                return status
            if i < attempts - 1:
                time.sleep(delay)
        logger.warning(
            "SIM %s still AVAILABLE after %s polls — deferring contact-info",
            serial_number, attempts,
        )
        return status

    # ── Activation ────────────────────────────────────────────────────────

    def _activation_payload(self, package_code, order_reference, country_code):
        residence = _iso_alpha2(
            country_code or self.config.default_country_of_residence,
            default="GB",
        )
        payload = {
            "ratePlan": self.config.rate_plan,
            "externalReference": order_reference,
            "subscriberCountryOfResidence": residence,
            "group": self.config.group,
        }
        if package_code:
            payload["options"] = [{"name": package_code, "value": "on"}]
        return payload

    def activate_subscriber(self, serial_number, package_code, order_reference,
                            country_code=None) -> dict:
        """Activate a SIM and add the plan package in one call."""
        endpoint = (
            _SUBSCRIBER_BASE + quote(str(serial_number), safe="") + "/activate"
        )
        payload = self._activation_payload(package_code, order_reference, country_code)
        logger.info("Transatel activate %s (%s)", serial_number, package_code)
        result = self.client.post(endpoint, payload)
        return result["data"]

    def modify_subscriber_add_package(self, serial_number, package_code,
                                      order_reference, country_code=None) -> dict:
        """Add a package to an already-activated SIM (activate fallback)."""
        endpoint = (
            _SUBSCRIBER_BASE + quote(str(serial_number), safe="") + "/modify"
        )
        payload = self._activation_payload(package_code, order_reference, country_code)
        payload.setdefault("options", [{"name": package_code, "value": "on"}])
        logger.info("Transatel modify %s (%s)", serial_number, package_code)
        result = self.client.post(endpoint, payload)
        return result["data"]

    # ── eSIM QR / LPA ─────────────────────────────────────────────────────

    def get_esim_qr(self, serial_number: str) -> dict:
        """Fetch the eSIM's QR / LPA profile for a SIM serial.

        Port of the plugin's ``get_esim_qr_code``:
            GET /sim-management/sims/api/esims/sim-serial/{serial}?history=false

        Returns a normalised dict (empty strings for anything not present yet):
            {activation_code, matching_id, smdp_address,
             qr_code_value, qr_code_data_url, status, status_date}

        ``qr_code_value`` is the LPA string (e.g. "LPA:1$smdp$MATCHINGID") and
        ``qr_code_data_url`` is a base64 PNG data URL of the QR. Both are empty
        until the eSIM profile has been provisioned, so callers should treat an
        empty ``qr_code_value`` as "not ready yet" and retry later.
        """
        endpoint = _ESIM_BASE + quote(str(serial_number), safe="")
        result = self.client.get(endpoint, params={"history": "false"})
        data = result.get("data") or {}
        qr = data.get("qrCode") or {}
        return {
            "activation_code": data.get("activationCode", "") or "",
            "matching_id": data.get("matchingId", "") or "",
            "smdp_address": data.get("smdpAddress", "") or "",
            "qr_code_value": qr.get("value", "") or "",
            "qr_code_data_url": qr.get("dataUrl", "") or "",
            "status": data.get("status", "") or "",
            "status_date": data.get("statusDate", "") or "",
        }

    # ── Contact info ──────────────────────────────────────────────────────

    def update_subscriber_contact(self, serial_number, subscriber_info: dict) -> dict:
        """Push the customer's contact details to the SIM.

        ``subscriber_info`` is the normalised block the order builds from the
        billing/shipping address (see ``build_subscriber_info`` below). Mirrors
        the plugin's ``updateSubscriberContactN``: a PUT to /contact-info with a
        ``subscriberInfo`` object. Note ``country`` here is ISO-3166 alpha-3
        (e.g. "GBR"), which is different from the alpha-2 country of residence
        sent on activation.
        """
        endpoint = (
            _SUBSCRIBER_BASE + quote(str(serial_number), safe="") + "/contact-info"
        )
        payload = {
            "ratePlan": self.config.rate_plan,
            "subscriberInfo": {
                "pointOfSale": subscriber_info.get("point_of_sale") or "newPointOfSale",
                "title": subscriber_info.get("title") or "Mr",
                "firstName": subscriber_info.get("first_name") or "",
                "lastName": subscriber_info.get("last_name") or "",
                "dateOfBirth": subscriber_info.get("date_of_birth") or "",
                "address": subscriber_info.get("address") or "",
                "zipCode": subscriber_info.get("zip_code") or "",
                "city": subscriber_info.get("city") or "",
                "country": subscriber_info.get("country") or "GBR",
                "contactEmail": subscriber_info.get("email") or "",
            },
        }
        logger.info("Transatel contact-info %s", serial_number)
        result = self.client.put(endpoint, payload)
        return result["data"]

    # ── Orchestration ─────────────────────────────────────────────────────

    def activate_or_modify(self, serial_number, package_code, order_reference,
                           country_code=None) -> dict:
        """Activate a SIM; if it is already active elsewhere, add the package
        via /modify instead. Mirrors the plugin's activate->modify fallback.

        Returns the API response dict. Raises ``TransatelAPIError`` if neither
        path succeeds.
        """
        try:
            return self.activate_subscriber(
                serial_number, package_code, order_reference, country_code
            )
        except TransatelAPIError as exc:
            message = str(exc).lower()
            already_active = (
                "already activated" in message
                or "already active" in message
                or (exc.status_code in (409, 422))
            )
            if not already_active:
                raise
            logger.info(
                "SIM %s already active — adding package via /modify", serial_number
            )
            return self.modify_subscriber_add_package(
                serial_number, package_code, order_reference, country_code
            )

    def activate_order_sim(self, serial_number, package_code, order_reference,
                           country_code=None, subscriber_info: dict | None = None) -> dict:
        """Full per-SIM order flow: check status -> activate/modify -> contact-info.

        This is the faithful port of the plugin's checkout path and the method
        the order API should call. It:

        1. Reads the subscriber's current status by SIM serial. This is the
           "check subscriber on MSISDN" step — the lookup also returns the
           MSISDN the SIM was pre-loaded with.
        2. Activates the SIM (status Available) or adds the package via /modify
           (status Active/Suspended), attaching the plan by its ``transatelID``
           / ``package_code``.
        3. If contact details were supplied, pushes them to /contact-info.

        Returns a dict::

            {
              "msisdn": "...",             # from the pre-activation lookup
              "prior_status": "Available", # subscriber status before we touched it
              "already_active": False,
              "activation": { ...Transatel activate/modify response... },
              "contact": { ...contact-info response... } | None,
              "transaction_id": "...",     # best-effort, if the API returns one
            }

        Raises ``TransatelAPIError`` on a hard failure (activation itself
        failing). A failed contact-info push is logged and surfaced in the
        returned dict but does NOT roll back the activation, matching the
        plugin's "activate first, then best-effort contact update" behaviour.
        """
        msisdn = ""
        prior_status = ""
        try:
            current = self.get_subscriber_by_serial(serial_number)
            if isinstance(current, dict):
                msisdn = str(current.get("msisdn", "") or "")
                prior_status = str(current.get("status", "") or "")
        except TransatelAPIError as exc:
            # A brand-new park SIM may 400/404 on lookup before first activation.
            # That's fine — treat it as "Available" and let /activate decide.
            logger.info(
                "Subscriber lookup for %s returned %s — assuming Available",
                serial_number, getattr(exc, "status_code", "error"),
            )

        already_active = prior_status.strip().lower() in _ACTIVE_STATUSES

        if already_active:
            activation = self.modify_subscriber_add_package(
                serial_number, package_code, order_reference, country_code
            )
        else:
            # activate_or_modify still guards against a race where the SIM went
            # active between our lookup and this call.
            activation = self.activate_or_modify(
                serial_number, package_code, order_reference, country_code
            )

        tx_id = ""
        if isinstance(activation, dict):
            tx_id = str(activation.get("transactionId", "") or "")
            if not msisdn:
                msisdn = str(activation.get("msisdn", "") or "")

        # After a fresh /activate the SIM is still AVAILABLE for a few seconds
        # (async provisioning). /contact-info is rejected while AVAILABLE, so
        # wait for the status to move before pushing it.
        final_status = prior_status
        if not already_active:
            final_status = self.wait_until_provisioned(serial_number) or prior_status

        contact = None
        if subscriber_info:
            still_available = (final_status or "").strip().lower() in ("", "available")
            if still_available:
                # Provisioning hasn't finished within the poll window. Don't
                # error — the activation itself succeeded. Report the contact
                # push as deferred so a later retry can complete it.
                logger.info(
                    "Deferring contact-info for %s (status still %r)",
                    serial_number, final_status,
                )
                contact = {
                    "status": "pending",
                    "detail": (
                        "SIM still provisioning (status AVAILABLE); "
                        "contact-info deferred until active."
                    ),
                }
            else:
                try:
                    resp = self.update_subscriber_contact(serial_number, subscriber_info)
                    contact = {"status": "success", "data": resp}
                except TransatelAPIError as exc:
                    logger.error(
                        "Contact-info update failed for %s: %s", serial_number, exc
                    )
                    contact = {"status": "error", "detail": str(exc)}

        return {
            "msisdn": msisdn,
            "prior_status": prior_status,
            "post_status": final_status,
            "already_active": already_active,
            "activation": activation,
            "contact": contact,
            "transaction_id": tx_id,
        }


def build_subscriber_info(order: dict) -> dict:
    """Normalise a checkout payload's address into contact-info fields.

    Accepts the ``SimOrderSerializer`` payload. Precedence:
      1. an already-normalised ``subscriber`` block (used as-is);
      2. otherwise, whatever is on the billing / shipping address — billing
         wins per field, shipping fills any gaps.

    Anything missing on both is left blank so we only send what we actually
    have, rather than dummy values.
    """
    if not isinstance(order, dict):
        return {}

    # Already-normalised block wins.
    sub = order.get("subscriber")
    if isinstance(sub, dict) and sub:
        return sub

    billing = order.get("billing") or order.get("billingAddress") or {}
    shipping = order.get("shipping") or order.get("shippingAddress") or {}
    billing = billing if isinstance(billing, dict) else {}
    shipping = shipping if isinstance(shipping, dict) else {}
    if not billing and not shipping:
        return {}

    def pick(*keys):
        """First non-empty value for any of `keys`, billing before shipping."""
        for src in (billing, shipping):
            for k in keys:
                v = src.get(k)
                if v not in (None, ""):
                    return v
        return ""

    house = str(pick("houseNumber", "house_number") or "").strip()
    street = str(pick("street") or "").strip()
    address = " ".join(part for part in (house, street) if part)

    return {
        "title": pick("title") or "Mr",
        "first_name": pick("firstName", "first_name"),
        "last_name": pick("lastName", "last_name"),
        "date_of_birth": pick("dateOfBirth", "dob", "date_of_birth"),
        "address": address or pick("address"),
        "zip_code": pick("zip", "zip_code", "zipCode"),
        "city": pick("city"),
        # contact-info wants ISO alpha-3; default to GBR like the plugin.
        "country": pick("country") or "GBR",
        "email": pick("email") or order.get("email") or "",
    }


def get_service() -> TransatelService:
    return TransatelService()