"""Authenticated HTTP client.

Port of the plugin's ``Core\\API_Client``. Attaches the bearer token from the
token manager, sends/receives JSON, and normalises the response into a dict
of ``{success, status_code, data}``. On a 401 it refreshes the token once and
retries (the plugin relied on the cron/init refresh; here we do it inline).
"""

from __future__ import annotations

import logging

import requests

from .config import TransatelConfig, get_config
from .exceptions import TransatelAPIError
from .token_manager import TokenManager

logger = logging.getLogger("apps.sims.transatel")


class APIClient:
    def __init__(self, token_manager: TokenManager | None = None,
                 config: TransatelConfig | None = None):
        self.config = config or get_config()
        self.token_manager = token_manager or TokenManager(self.config)

    # ── Verb helpers ──────────────────────────────────────────────────────

    def get(self, endpoint, params=None):
        return self.request("GET", endpoint, params=params)

    def post(self, endpoint, data=None):
        return self.request("POST", endpoint, json_body=data)

    def put(self, endpoint, data=None):
        return self.request("PUT", endpoint, json_body=data)

    def patch(self, endpoint, data=None):
        return self.request("PATCH", endpoint, json_body=data)

    def delete(self, endpoint):
        return self.request("DELETE", endpoint)

    # ── Core ──────────────────────────────────────────────────────────────

    def request(self, method, endpoint, params=None, json_body=None, _retry=True):
        url = self.config.get_api_url(endpoint)
        headers = {
            "Authorization": self.token_manager.get_auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Django/Transatel-Integration",
        }

        try:
            resp = requests.request(
                method.upper(),
                url,
                headers=headers,
                params=params or None,
                json=json_body if json_body is not None else None,
                timeout=self.config.request_timeout,
            )
        except requests.RequestException as exc:
            logger.error("Transatel %s %s failed: %s", method, endpoint, exc)
            raise TransatelAPIError(f"Request failed: {exc}") from exc

        # Refresh-and-retry once on an expired/invalid token.
        if resp.status_code == 401 and _retry:
            logger.info("Transatel 401 — refreshing token and retrying once")
            self.token_manager.force_refresh()
            return self.request(method, endpoint, params, json_body, _retry=False)

        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {"raw": resp.text}

        success = 200 <= resp.status_code < 300
        if not success:
            detail = ""
            if isinstance(data, dict):
                detail = data.get("detail") or data.get("message") or ""
            raise TransatelAPIError(
                detail or f"{method} {endpoint} returned {resp.status_code}",
                status_code=resp.status_code,
                payload=json_body,
                response=data,
            )

        return {"success": True, "status_code": resp.status_code, "data": data}
