"""High-level subscriber operations.

Port of the activation logic in the plugin's WooCommerce integration
(``get_subscriber_by_serial`` / ``activate_subscriber`` /
``modify_subscriber_add_package``). These are the only three Transatel calls
the order flow needs.

Endpoints (relative to the API base URL):

    GET  /connectivity-management/subscribers/api/subscribers/sim-serial/{serial}
    POST /connectivity-management/subscribers/api/subscribers/sim-serial/{serial}/activate
    POST /connectivity-management/subscribers/api/subscribers/sim-serial/{serial}/modify

``package_code`` is the plan's Transatel product code (e.g. ``TSL_UK_DATA_1GB``)
and is sent as an activation *option* — matching the plugin, which passes the
product code as ``options[].name`` with ``value: "on"``.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from .client import APIClient
from .config import TransatelConfig, get_config
from .exceptions import TransatelAPIError

logger = logging.getLogger("apps.sims.transatel")

_SUBSCRIBER_BASE = "/connectivity-management/subscribers/api/subscribers/sim-serial/"


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

    # ── Activation ────────────────────────────────────────────────────────

    def _activation_payload(self, package_code, order_reference, country_code):
        payload = {
            "ratePlan": self.config.rate_plan,
            "externalReference": order_reference,
            "subscriberCountryOfResidence": country_code
            or self.config.default_country_of_residence,
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

    # ── Orchestration ─────────────────────────────────────────────────────

    def activate_or_modify(self, serial_number, package_code, order_reference,
                           country_code=None) -> dict:
        """Activate a SIM; if it is already active elsewhere, add the package
        via /modify instead. Mirrors the plugin's activate→modify fallback.

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


def get_service() -> TransatelService:
    return TransatelService()
