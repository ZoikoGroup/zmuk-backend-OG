"""Guards for webhook secrets and webhook request shape.

Outside DEBUG a placeholder-looking secret counts as unset, so a webhook
fails closed (503) instead of running with a guessable secret.
"""

from django.conf import settings

_PLACEHOLDER_HINTS = ("xxxx", "changeme", "change_me", "placeholder", "your_", "your-", "dummy", "example")
MIN_SECRET_LENGTH = 16


def usable_secret(value) -> str:
    value = (value or "").strip()
    if not value or settings.DEBUG:
        return value
    lowered = value.lower()
    if len(value) < MIN_SECRET_LENGTH or any(hint in lowered for hint in _PLACEHOLDER_HINTS):
        return ""
    return value


def request_problem(request, max_bytes: int):
    """(status, reason) when a webhook request is the wrong shape, else None."""
    content_type = (request.META.get("CONTENT_TYPE") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return 415, "Unsupported content type"
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return 400, "Invalid content length"
    if declared > max_bytes or len(request.body) > max_bytes:
        return 413, "Payload too large"
    return None
