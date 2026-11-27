"""Fingerprints and encryption for SIM change data (CTO response §7).

* `fingerprint` is a keyed HMAC-SHA256 of a normalised identifier (ICCID,
  MSISDN). Cases store fingerprints so two values can be compared without
  keeping the raw identifier.
* `encrypt_json` / `decrypt_json` protect the few values we must keep, such
  as the delivery address snapshot. Separate key from PAC encryption.

Both fail closed: a missing or invalid key raises `CryptoUnavailable` rather
than storing anything in the clear.
"""

import hashlib
import hmac
import json
import re

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class CryptoUnavailable(Exception):
    """A key is missing or wrong; nothing sensitive may be stored."""


def _normalise(value) -> str:
    # ICCIDs and MSISDNs: compare digits only (spaces, '+', dashes ignored).
    return re.sub(r"\D", "", str(value or ""))


def fingerprint(value) -> str:
    """Keyed fingerprint of an identifier; '' for an empty value."""
    normalised = _normalise(value)
    if not normalised:
        return ""
    key = getattr(settings, "SIM_CHANGE_FINGERPRINT_KEY", "") or ""
    if len(key) < 32:
        raise CryptoUnavailable("SIM_CHANGE_FINGERPRINT_KEY is not set (need 32+ characters)")
    return hmac.new(key.encode(), normalised.encode(), hashlib.sha256).hexdigest()


def text_fingerprint(value: str) -> str:
    """Keyed fingerprint of free text (already normalised by the caller)."""
    if not value:
        return ""
    key = getattr(settings, "SIM_CHANGE_FINGERPRINT_KEY", "") or ""
    if len(key) < 32:
        raise CryptoUnavailable("SIM_CHANGE_FINGERPRINT_KEY is not set (need 32+ characters)")
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()


def same_text(a: str, b: str) -> bool:
    """Constant-time comparison of two hashes / tokens."""
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


def same_identifier(stored_fingerprint: str, value) -> bool:
    """Constant-time check of a raw identifier against a stored fingerprint."""
    if not stored_fingerprint or not _normalise(value):
        return False
    return hmac.compare_digest(stored_fingerprint, fingerprint(value))


def _fernet() -> Fernet:
    key = getattr(settings, "SIM_CHANGE_ENCRYPTION_KEY", "") or ""
    if not key:
        raise CryptoUnavailable("SIM_CHANGE_ENCRYPTION_KEY is not set")
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise CryptoUnavailable("SIM_CHANGE_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt_json(data) -> str:
    return _fernet().encrypt(json.dumps(data, sort_keys=True).encode()).decode()


def decrypt_json(token: str):
    if not token:
        return None
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except InvalidToken as exc:
        raise CryptoUnavailable("stored value could not be decrypted (key changed?)") from exc


def mask_serial(value) -> str:
    """Last 4 digits only, for evidence and admin screens."""
    digits = _normalise(value)
    return f"…{digits[-4:]}" if digits else ""
