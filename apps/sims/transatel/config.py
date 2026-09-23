"""Configuration for the Transatel API.

Port of the plugin's ``Core\\Config`` class. Instead of a WordPress option
row, settings come from Django settings (or the environment as a fallback),
so nothing secret is committed to the repo.

Read, in order of precedence:

1. ``settings.TRANSATEL`` — a dict, e.g. ::

       TRANSATEL = {
           "API_BASE_URL": "https://api.transatel.com",
           "AUTH_ENDPOINT": "/authentication/api/token",
           "CLIENT_ID": os.environ["TRANSATEL_CLIENT_ID"],
           "CLIENT_SECRET": os.environ["TRANSATEL_CLIENT_SECRET"],
           "RATE_PLAN": "MVNA Wholesale PAYM 7",
           "GROUP": "ZOIKO eSIM",
           "DEFAULT_COUNTRY_OF_RESIDENCE": "UK",
           "TOKEN_REFRESH_BUFFER": 300,
           "REQUEST_TIMEOUT": 30,
       }

2. Environment variables ``TRANSATEL_*`` (same keys).
3. Built-in defaults (matching the plugin's defaults).
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

try:  # Django is optional at import time so the module can be unit-tested standalone.
    from django.conf import settings as _dj_settings
except Exception:  # pragma: no cover
    _dj_settings = None


_DEFAULTS = {
    "API_BASE_URL": "https://api.transatel.com",
    "AUTH_ENDPOINT": "/authentication/api/token",
    "CLIENT_ID": "",
    "CLIENT_SECRET": "",
    # Wholesale rate plan applied on activation (plugin hard-codes this).
    "RATE_PLAN": "MVNA Wholesale PAYM 7",
    # Subscriber group tag sent with activate/modify. This MUST be a group
    # Transatel has provisioned for your account; an unknown value makes the
    # activation transaction fail asynchronously (SIM stays AVAILABLE). The
    # plugin used "Group A1" — override via settings.TRANSATEL / TRANSATEL_GROUP.
    "GROUP": "Group A1",
    # ISO country of residence used when the order doesn't supply one.
    "DEFAULT_COUNTRY_OF_RESIDENCE": "UK",
    # Refresh the token this many seconds before it actually expires.
    "TOKEN_REFRESH_BUFFER": 300,
    "REQUEST_TIMEOUT": 30,
}


def _lookup(key: str):
    """Resolve a single setting from settings.TRANSATEL, then env, then default."""
    if _dj_settings is not None:
        block = getattr(_dj_settings, "TRANSATEL", None)
        if isinstance(block, dict) and key in block and block[key] not in (None, ""):
            return block[key]
    env_val = os.environ.get(f"TRANSATEL_{key}")
    if env_val not in (None, ""):
        return env_val
    return _DEFAULTS[key]


@dataclass
class TransatelConfig:
    api_base_url: str
    auth_endpoint: str
    client_id: str
    client_secret: str
    rate_plan: str
    group: str
    default_country_of_residence: str
    token_refresh_buffer: int
    request_timeout: int

    @classmethod
    def load(cls) -> "TransatelConfig":
        return cls(
            api_base_url=str(_lookup("API_BASE_URL")).rstrip("/"),
            auth_endpoint="/" + str(_lookup("AUTH_ENDPOINT")).lstrip("/"),
            client_id=str(_lookup("CLIENT_ID")),
            client_secret=str(_lookup("CLIENT_SECRET")),
            rate_plan=str(_lookup("RATE_PLAN")),
            group=str(_lookup("GROUP")),
            default_country_of_residence=str(_lookup("DEFAULT_COUNTRY_OF_RESIDENCE")),
            token_refresh_buffer=int(_lookup("TOKEN_REFRESH_BUFFER")),
            request_timeout=int(_lookup("REQUEST_TIMEOUT")),
        )

    # ── Helpers (mirror the plugin's Config) ──────────────────────────────

    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def get_auth_url(self) -> str:
        return self.api_base_url + self.auth_endpoint

    def get_api_url(self, endpoint: str) -> str:
        return self.api_base_url + "/" + endpoint.lstrip("/")

    def get_basic_auth(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        return base64.b64encode(raw).decode("ascii")


def get_config() -> TransatelConfig:
    """Build a fresh config from current settings/env."""
    return TransatelConfig.load()