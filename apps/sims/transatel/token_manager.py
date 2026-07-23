"""OAuth2 token manager.

Port of the plugin's ``Core\\Token_Manager``. Uses the client-credentials
grant with HTTP Basic auth, caches the access token in Django's cache
backend, and refreshes it a configurable buffer before expiry.

The cached value stores the absolute expiry timestamp so a token is only
reused while it has more than ``token_refresh_buffer`` seconds of life left —
exactly like ``is_token_valid()`` in the plugin.
"""

from __future__ import annotations

import logging
import time

import requests

from .config import TransatelConfig, get_config
from .exceptions import TransatelAuthError, TransatelNotConfigured

logger = logging.getLogger("apps.sims.transatel")

try:
    from django.core.cache import cache as _dj_cache
except Exception:  # pragma: no cover - allows standalone testing
    _dj_cache = None


_CACHE_KEY = "transatel:access_token"
# Fallback in-process store when Django cache isn't available.
_memory_store: dict = {}


def _cache_get(key):
    if _dj_cache is not None:
        return _dj_cache.get(key)
    entry = _memory_store.get(key)
    if not entry:
        return None
    value, expires = entry
    if expires is not None and expires < time.time():
        _memory_store.pop(key, None)
        return None
    return value


def _cache_set(key, value, ttl):
    if _dj_cache is not None:
        _dj_cache.set(key, value, timeout=ttl)
    else:
        _memory_store[key] = (value, time.time() + ttl if ttl else None)


def _cache_delete(key):
    if _dj_cache is not None:
        _dj_cache.delete(key)
    else:
        _memory_store.pop(key, None)


class TokenManager:
    def __init__(self, config: TransatelConfig | None = None):
        self.config = config or get_config()

    # ── Public API ────────────────────────────────────────────────────────

    def get_token(self, force_refresh: bool = False) -> str:
        """Return a valid bearer token, generating a new one if needed."""
        if not force_refresh:
            cached = _cache_get(_CACHE_KEY)
            if cached and self._is_valid(cached):
                return cached["access_token"]
        return self.generate_token()

    def get_auth_header(self) -> str:
        token = self.get_token()
        token_type = "Bearer"
        cached = _cache_get(_CACHE_KEY)
        if cached and cached.get("token_type"):
            token_type = cached["token_type"].capitalize()
        return f"{token_type} {token}"

    def force_refresh(self) -> str:
        _cache_delete(_CACHE_KEY)
        return self.generate_token()

    # ── Internals ─────────────────────────────────────────────────────────

    def generate_token(self) -> str:
        if not self.config.has_credentials():
            raise TransatelNotConfigured(
                "Transatel client_id / client_secret are not configured."
            )

        auth_url = self.config.get_auth_url()
        try:
            resp = requests.post(
                auth_url,
                headers={
                    "Authorization": "Basic " + self.config.get_basic_auth(),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={"grant_type": "client_credentials"},
                timeout=self.config.request_timeout,
            )
        except requests.RequestException as exc:
            logger.error("Token generation request failed: %s", exc)
            raise TransatelAuthError(f"Token request failed: {exc}") from exc

        if resp.status_code != 200:
            try:
                detail = resp.json().get("error_description") or resp.text
            except ValueError:
                detail = resp.text
            logger.error("Token generation failed (%s): %s", resp.status_code, detail)
            raise TransatelAuthError(
                f"Token generation failed with status {resp.status_code}: {detail}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise TransatelAuthError("Token response was not valid JSON.") from exc

        access_token = data.get("access_token")
        if not access_token:
            raise TransatelAuthError("Token response contained no access_token.")

        expires_in = int(data.get("expires_in", 3600))
        now = int(time.time())
        token_data = {
            "access_token": access_token,
            "token_type": data.get("token_type", "bearer"),
            "scope": data.get("scope", ""),
            "expires_at": now + expires_in,
        }

        # Cache only for the usable window (expiry minus the refresh buffer),
        # so the cache itself enforces the buffer even without a re-check.
        ttl = max(expires_in - self.config.token_refresh_buffer, 1)
        _cache_set(_CACHE_KEY, token_data, ttl)

        logger.info("Transatel token generated, expires in %ss", expires_in)
        return access_token

    def _is_valid(self, token_data: dict) -> bool:
        expires_at = token_data.get("expires_at")
        if not expires_at:
            return False
        buffer = self.config.token_refresh_buffer
        return (expires_at - buffer) > int(time.time())
