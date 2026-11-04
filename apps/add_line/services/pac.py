"""PAC / STAC handling: format check, UK number normalisation, encryption.

Only the *format* can be checked before the request reaches Transatel (B8);
the network validates the code asynchronously, so a passing format check must
never be shown as "PAC verified".

Codes are stored Fernet-encrypted with PAC_ENCRYPTION_KEY and blanked by
`purge_pac_codes` once no longer needed (C13).
"""

import re

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

PAC = "PAC"
STAC = "STAC"

# Ofcom formats: PAC = 3 letters + 6 digits, STAC = 6 digits + 3 letters.
_FORMATS = {
    PAC: re.compile(r"^[A-Z]{3}\d{6}$"),
    STAC: re.compile(r"^\d{6}[A-Z]{3}$"),
}
_UK_MOBILE = re.compile(r"^447\d{9}$")


class PacError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class PacStorageUnavailable(Exception):
    """PAC_ENCRYPTION_KEY is missing or invalid."""


def normalise_code(code_type: str, code: str) -> str:
    cleaned = re.sub(r"\s+", "", code or "").upper()
    if not _FORMATS[code_type].match(cleaned):
        example = "ABC123456" if code_type == PAC else "123456ABC"
        raise PacError(f"{code_type}_FORMAT_INVALID",
                       f"That doesn't look like a {code_type}. It should look like {example}.")
    return cleaned


def normalise_msisdn(number: str) -> str:
    """UK mobile number → 447XXXXXXXXX (the format Transatel expects)."""
    digits = re.sub(r"[^\d]", "", number or "")
    if digits.startswith("07") and len(digits) == 11:
        digits = "44" + digits[1:]
    elif digits.startswith("00447"):
        digits = digits[2:]
    if not _UK_MOBILE.match(digits):
        raise PacError("MSISDN_INVALID", "Enter the UK mobile number you're moving, e.g. 07700 900123.")
    return digits


def _fernet() -> Fernet:
    key = settings.PAC_ENCRYPTION_KEY
    if not key:
        raise PacStorageUnavailable("PAC_ENCRYPTION_KEY is not set")
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise PacStorageUnavailable("PAC_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt(code: str) -> str:
    return _fernet().encrypt(code.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise PacStorageUnavailable("stored PAC could not be decrypted (key changed?)") from exc
