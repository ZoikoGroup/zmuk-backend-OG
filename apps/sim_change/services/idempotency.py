"""Idempotency-Key handling for customer writes (CTO v2.0 §8.1).

* Same key + same body → the stored response is returned (double clicks,
  retries after a timeout).
* Same key + different body → rejected (IDEMPOTENCY_KEY_REUSED).
* Same key while the first request is still running → REQUEST_IN_PROGRESS.
* A request that fails is forgotten, so the customer can fix it and retry
  with the same key.
"""

import hashlib
import json
import re

from django.db import IntegrityError, transaction

from ..models import IdempotencyRecord, current_tenant
from .transitions import TransitionError

_KEY = re.compile(r"^[A-Za-z0-9_\-:.]{8,128}$")


class IdempotencyError(TransitionError):
    pass


def digest(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def begin(user, scope: str, key: str, body) -> IdempotencyRecord | dict:
    """Returns a new record to finish later, or the stored response dict for a replay."""
    key = (key or "").strip()
    if not _KEY.match(key):
        raise IdempotencyError("IDEMPOTENCY_KEY_REQUIRED", "Send an Idempotency-Key header (8–128 characters).", 422)
    request_digest = digest(body)
    try:
        with transaction.atomic():
            return IdempotencyRecord.objects.create(
                tenant=current_tenant(), user=user, scope=scope, key=key, request_digest=request_digest,
            )
    except IntegrityError:
        existing = IdempotencyRecord.objects.get(tenant=current_tenant(), user=user, scope=scope, key=key)
    if existing.request_digest != request_digest:
        raise IdempotencyError(
            "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was already used for a different request.", 422,
        )
    if existing.response is None:
        raise IdempotencyError("REQUEST_IN_PROGRESS", "This request is already being processed.", 409)
    return {"status_code": existing.status_code, "body": existing.response}


def finish(record: IdempotencyRecord, status_code: int, body: dict):
    record.status_code = status_code
    record.response = body
    record.save(update_fields=["status_code", "response"])


def forget(record: IdempotencyRecord):
    IdempotencyRecord.objects.filter(pk=record.pk).delete()
