"""Transatel webhook receiver (decision B9).

POST /api/v1/lines/webhooks/transatel/

    1. verify X-TSL-Signature-256 = "sha256=" + HMAC_SHA256(secret, raw body),
       constant-time, before parsing anything;
    2. store the event under its unique header.eventId — a retried delivery
       hits the unique constraint and is acknowledged without reprocessing;
    3. acknowledge 204, then apply the event. Processing errors are recorded on
       the SupplierEvent row rather than returned, so Transatel doesn't retry
       an event we already hold.

Only the exact event types in the Transatel UK event catalogue are acted on
(engineering decision ZMUK-TSL-ADDLINE-004 §05); anything else is stored and
ignored. Payloads are searched for `transactionId` / `simSerial` wherever they
sit until tenant fixtures confirm the layout. Port events match on the
transaction id and must agree with the request's PAC/STAC type and SIM;
SUBSCRIBER/ACTIVATED only triggers a status re-check — it is never trusted on
its own. There is no LOCKED event: that status has to be polled.
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt

from .models import AddLineOrder, Line, PortRequest, SupplierEvent
from .services import fulfilment, porting
from .services.secrets import request_problem, usable_secret

logger = logging.getLogger("apps.add_line")

_SIGNATURE_HEADER = "HTTP_X_TSL_SIGNATURE_256"
_PORT_DATE_KEYS = ("portabilityDate", "portingDate", "portDate")
MAX_BODY_BYTES = 64 * 1024

SUBSCRIBER_ACTIVATED = "CONNECTIVITY-MANAGEMENT/SUBSCRIBER/ACTIVATED"
_PORTABILITY_PREFIX = "CONNECTIVITY-MANAGEMENT/PORTABILITY/UK/"
# PORT_IN carries PAC requests, SWITCH_IN carries STAC requests.
_FLOW_CODE_TYPE = {"PORT_IN": PortRequest.CodeType.PAC, "SWITCH_IN": PortRequest.CodeType.STAC}
_SUFFIX_STATUS = {
    "VALIDATED": PortRequest.Status.VALIDATED,
    "INVALIDATED": PortRequest.Status.INVALIDATED,
    "DONE": PortRequest.Status.DONE,
    "CANCELED": PortRequest.Status.CANCELED,
    "ATTACH/REQUESTED": PortRequest.Status.ATTACH_REQUESTED,
    "ATTACH/RECEIVED": PortRequest.Status.ATTACH_RECEIVED,
    "ATTACH/FAILED": PortRequest.Status.ATTACH_FAILED,
}
# Full event type → (code type it belongs to, port status it sets).
PORT_EVENTS = {
    f"{_PORTABILITY_PREFIX}{flow}/{suffix}": (code_type, status)
    for flow, code_type in _FLOW_CODE_TYPE.items()
    for suffix, status in _SUFFIX_STATUS.items()
}


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

    secret = usable_secret(settings.TRANSATEL_WEBHOOK_SECRET)
    if not secret:
        logger.error("Transatel webhook rejected: TRANSATEL_WEBHOOK_SECRET not set or a placeholder")
        return HttpResponse("Service unavailable", status=503)

    problem = request_problem(request, MAX_BODY_BYTES)
    if problem:
        return JsonResponse({"error": problem[1]}, status=problem[0])

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


def _sim_matches(port: PortRequest, serial) -> bool:
    sim = port.line.sim
    return bool(sim) and str(serial) in (sim.iccid, sim.serial_number)


def process(event: SupplierEvent):
    event_type = event.event_type.strip().upper()
    tx_id = _find(event.payload, "transactionId")
    serial = _find(event.payload, "simSerial")

    if event_type in PORT_EVENTS:
        code_type, status = PORT_EVENTS[event_type]
        port = None
        if tx_id:
            port = PortRequest.objects.select_related("line__sim").filter(
                supplier_transaction_id=str(tx_id)
            ).first()
        if not port and serial:
            port = PortRequest.objects.select_related("line__sim").filter(
                Q(line__sim__iccid=serial) | Q(line__sim__serial_number=serial),
            ).exclude(status__in=PortRequest.TERMINAL_STATUSES).first()
        if not port:
            return _finish(event, SupplierEvent.Status.IGNORED, error="no matching port request")
        # Identifiers that disagree are quarantined for review, never applied.
        if port.code_type != code_type:
            return _finish(event, SupplierEvent.Status.IGNORED, port=port,
                           error=f"{event_type} does not match a {port.code_type} request")
        if serial and not _sim_matches(port, serial):
            return _finish(event, SupplierEvent.Status.IGNORED, port=port,
                           error="simSerial does not match the request's SIM")

        port_date = next(
            (parse_date(str(d)) for d in (_find(event.payload, k) for k in _PORT_DATE_KEYS) if d), None
        )
        changed = porting.apply_status(port, status, port_date=port_date)
        return _finish(event, SupplierEvent.Status.PROCESSED if changed else SupplierEvent.Status.IGNORED,
                       port=port, error="" if changed else "stale or unknown status")

    if event_type == SUBSCRIBER_ACTIVATED and serial:
        line = Line.objects.filter(Q(sim__iccid=serial) | Q(sim__serial_number=serial)) \
            .exclude(status__in=[Line.Status.TERMINATED, Line.Status.CANCELLED]).first()
        order = getattr(line, "order", None) if line else None
        if order and order.fulfilment_status in (
            AddLineOrder.FulfilmentStatus.ACTIVATING, AddLineOrder.FulfilmentStatus.FAILED,
        ):
            fulfilment.fulfil(order)  # re-checks Transatel before marking live
            return _finish(event, SupplierEvent.Status.PROCESSED)

    return _finish(event, SupplierEvent.Status.IGNORED, error="no action for this event")
