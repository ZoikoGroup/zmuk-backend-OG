"""Transatel webhook receiver (decision B9).

POST /api/v1/lines/webhooks/transatel/

    1. verify X-TSL-Signature-256 = "sha256=" + HMAC_SHA256(secret, raw body),
       constant-time, before parsing anything;
    2. store the event under its unique header.eventId — a retried delivery
       hits the unique constraint and is acknowledged without reprocessing;
    3. acknowledge 204, then apply the event. Processing errors are recorded on
       the SupplierEvent row rather than returned, so Transatel doesn't retry
       an event we already hold.

Event payloads are searched for `transactionId` / `simSerial` wherever they
sit, since the exact body layout per event type is still to be confirmed with
Transatel. Port events match on the transaction id; SUBSCRIBER/ACTIVATED only
triggers a status re-check — it is never trusted on its own.
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt

from .models import AddLineOrder, Line, PortRequest, SupplierEvent
from .services import fulfilment, porting

logger = logging.getLogger("apps.add_line")

_SIGNATURE_HEADER = "HTTP_X_TSL_SIGNATURE_256"
_PORT_DATE_KEYS = ("portabilityDate", "portingDate", "portDate")


def signature_is_valid(body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def _find(data, key):
    """First value for `key` anywhere in a nested dict/list."""
    if isinstance(data, dict):
        if data.get(key) not in (None, ""):
            return data[key]
        values = data.values()
    elif isinstance(data, list):
        values = data
    else:
        return None
    for value in values:
        found = _find(value, key)
        if found not in (None, ""):
            return found
    return None


@csrf_exempt  # nosemgrep: no-csrf-exempt -- supplier webhook, verified via HMAC signature, not CSRF token
def transatel_webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)

    secret = settings.TRANSATEL_WEBHOOK_SECRET
    if not secret:
        logger.error("Transatel webhook rejected: TRANSATEL_WEBHOOK_SECRET not set")
        return HttpResponse("Service unavailable", status=503)

    body = request.body
    if not signature_is_valid(body, request.META.get(_SIGNATURE_HEADER, ""), secret):
        return HttpResponse("Invalid signature", status=401)

    try:
        payload = json.loads(body)
        header = payload["header"]
        event_id = str(header["eventId"])
        event_type = str(header["eventType"])
    except (ValueError, KeyError, TypeError):
        return HttpResponse("Invalid payload", status=400)

    try:
        with transaction.atomic():
            event = SupplierEvent.objects.create(
                event_id=event_id,
                event_type=event_type,
                event_date=parse_datetime(str(header.get("eventDate") or "")),
                payload=payload,
                payload_sha256=hashlib.sha256(body).hexdigest(),
            )
    except IntegrityError:
        return HttpResponse(status=204)  # duplicate delivery

    try:
        process(event)
    except Exception as exc:
        logger.exception("Processing Transatel event %s failed", event_id)
        event.status = SupplierEvent.Status.FAILED
        event.error = str(exc)[:2000]
        event.processed_at = timezone.now()
        event.save(update_fields=["status", "error", "processed_at"])
    return HttpResponse(status=204)


def _finish(event, status, *, port=None, error=""):
    event.status = status
    event.port_request = port
    event.error = error
    event.processed_at = timezone.now()
    event.save(update_fields=["status", "port_request", "error", "processed_at"])


def process(event: SupplierEvent):
    parts = event.event_type.upper().split("/")
    status = parts[-1]
    tx_id = _find(event.payload, "transactionId")
    serial = _find(event.payload, "simSerial")

    if any("PORT" in p or "SWITCH" in p for p in parts[:-1]):
        port = None
        if tx_id:
            port = PortRequest.objects.filter(supplier_transaction_id=str(tx_id)).first()
        if not port and serial:
            port = PortRequest.objects.filter(
                Q(line__sim__iccid=serial) | Q(line__sim__serial_number=serial),
            ).exclude(status__in=PortRequest.TERMINAL_STATUSES).first()
        if not port:
            return _finish(event, SupplierEvent.Status.IGNORED, error="no matching port request")

        port_date = next(
            (parse_date(str(d)) for d in (_find(event.payload, k) for k in _PORT_DATE_KEYS) if d), None
        )
        changed = porting.apply_status(port, status, port_date=port_date)
        return _finish(event, SupplierEvent.Status.PROCESSED if changed else SupplierEvent.Status.IGNORED,
                       port=port, error="" if changed else "stale or unknown status")

    if "SUBSCRIBER" in parts and status == "ACTIVATED" and serial:
        line = Line.objects.filter(Q(sim__iccid=serial) | Q(sim__serial_number=serial)) \
            .exclude(status__in=[Line.Status.TERMINATED, Line.Status.CANCELLED]).first()
        order = getattr(line, "order", None) if line else None
        if order and order.fulfilment_status in (
            AddLineOrder.FulfilmentStatus.ACTIVATING, AddLineOrder.FulfilmentStatus.FAILED,
        ):
            fulfilment.fulfil(order)  # re-checks Transatel before marking live
            return _finish(event, SupplierEvent.Status.PROCESSED)

    return _finish(event, SupplierEvent.Status.IGNORED, error="no action for this event")
