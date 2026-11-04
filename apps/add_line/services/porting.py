"""Switching a number into Zoiko with a PAC or STAC (decisions B7, C12, C14).

* The customer's old provider issues the code; the customer gives it to us.
* It is only sent to Transatel once the new line is ACTIVE. Until then the
  request waits in AWAITING_ACTIVATION.
* Transatel's 201 is PENDING, not accepted. The real outcome arrives as
  events and is applied by `apply_status`, which never moves a request
  backwards (events can arrive late, twice or out of order).
* A failed or invalid code never touches the new line: it keeps working and
  the customer can submit a new code.
"""

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.sims.transatel import TransatelAPIError, TransatelError, get_service

from ..models import Line, PortRequest
from . import audit, pac

logger = logging.getLogger("apps.add_line")

S = PortRequest.Status

# Forward-only progress; anything not listed is a terminal or side state.
_PROGRESS = [
    S.AWAITING_ACTIVATION, S.PENDING, S.ATTACH_REQUESTED, S.ATTACH_RECEIVED,
    S.VALIDATED, S.LOCKED, S.DONE,
]
# Supplier HTTP errors worth retrying; other 4xx mean the request itself is wrong.
_RETRYABLE_HTTP = {401, 408, 409, 423, 429}


class PortError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def create_request(user, line: Line, *, code_type, code, msisdn, wished_date=None) -> PortRequest:
    if line.status not in (Line.Status.PENDING_ACTIVATION, Line.Status.ACTIVE):
        raise PortError("LINE_NOT_ELIGIBLE", "This line can't receive a number transfer.")

    # The customer picked PAC, STAC or "no switch" at checkout; STAC ends their
    # old contract, so it is only ever sent when they expressly chose it.
    order = getattr(line, "order", None)
    if order and order.switch_choice != code_type.lower():
        raise PortError("SWITCH_NOT_SELECTED",
                        f"You didn't choose a {code_type} switch for this number. Please contact support to change it.")

    try:
        code = pac.normalise_code(code_type, code)
        msisdn = pac.normalise_msisdn(msisdn)
    except pac.PacError as exc:
        raise PortError(exc.code, exc.message)

    if wished_date and wished_date <= timezone.localdate():
        raise PortError("WISHED_DATE_INVALID", "Choose a transfer date in the future.")

    try:
        encrypted = pac.encrypt(code)
    except pac.PacStorageUnavailable:
        logger.error("PAC_ENCRYPTION_KEY missing/invalid; cannot accept switch codes")
        raise PortError("PORTING_UNAVAILABLE", "Number transfers aren't available right now.", 503)

    try:
        with transaction.atomic():
            port = PortRequest.objects.create(
                line=line, order=order, code_type=code_type, encrypted_code=encrypted,
                portability_msisdn=msisdn, wished_date=wished_date,
            )
    except IntegrityError:
        raise PortError("PORT_IN_PROGRESS", "A transfer for this line is already in progress.", 409)

    audit.record("port_requested", line=line, port_request=port, actor=user, after=port.status,
                 code_type=code_type)
    if line.status == Line.Status.ACTIVE:
        submit(port)
        port.refresh_from_db()
    return port


def submit(port: PortRequest) -> PortRequest:
    """Send a waiting request to Transatel, once. Safe to call repeatedly."""
    with transaction.atomic():
        # Lock only this row: FOR UPDATE can't cover the nullable line→sim join.
        port = PortRequest.objects.select_for_update().get(pk=port.pk)
        line = port.line
        if port.status != S.AWAITING_ACTIVATION or line.status != Line.Status.ACTIVE or not line.sim:
            return port
        if not port.encrypted_code:
            # Purged before the line went live (code older than its 30-day life).
            _set_status(port, S.ERROR, error={"detail": "switch code expired before submission"})
            return port

        try:
            code = pac.decrypt(port.encrypted_code)
            data = get_service().request_uk_port_in(
                line.sim.serial_number or line.sim.iccid, port.portability_msisdn,
                port.code_type, code, port.wished_date,
            ) or {}
        except TransatelAPIError as exc:
            error = {"status_code": exc.status_code, "detail": str(exc), "response": exc.response}
            if exc.status_code and 400 <= exc.status_code < 500 and exc.status_code not in _RETRYABLE_HTTP:
                _set_status(port, S.ERROR, error=error)
            else:
                port.last_error = error
                port.save(update_fields=["last_error", "updated_at"])
            logger.warning("Port-in submission failed for line %s: %s", line.uuid, exc)
            return port
        except (TransatelError, pac.PacStorageUnavailable) as exc:
            port.last_error = {"detail": str(exc)}
            port.save(update_fields=["last_error", "updated_at"])
            logger.error("Port-in submission not possible for line %s: %s", line.uuid, exc)
            return port

        port.supplier_transaction_id = str(data.get("transactionId") or "")
        port.submitted_at = timezone.now()
        port.last_error = None
        port.save(update_fields=["supplier_transaction_id", "submitted_at", "last_error", "updated_at"])
        supplier_status = str(data.get("transactionStatus") or S.PENDING).upper()
        _set_status(port, supplier_status if supplier_status in S.values else S.PENDING, source="submit")
    return port


def submit_waiting(line: Line):
    """Called when a line goes live: send any request that was waiting for it."""
    for port in line.port_requests.filter(status=S.AWAITING_ACTIVATION):
        submit(port)


def _set_status(port: PortRequest, new_status: str, *, port_date=None, error=None, source="system"):
    before = port.status
    port.status = new_status
    now = timezone.now()
    fields = ["status", "updated_at"]
    if error is not None:
        port.last_error = error
        fields.append("last_error")
    if port_date:
        port.port_date = port_date
        fields.append("port_date")
    stamp = {S.VALIDATED: "validated_at", S.LOCKED: "locked_at", S.DONE: "completed_at"}.get(new_status)
    if stamp:
        setattr(port, stamp, now)
        fields.append(stamp)
    port.save(update_fields=fields)

    if new_status == S.DONE and port.code_type == PortRequest.CodeType.PAC:
        _switch_number(port)
    audit.record("port_status", line=port.line, port_request=port, before=before, after=new_status,
                 source=source)


def _switch_number(port: PortRequest):
    """PAC completed: the ported number replaces the temporary one."""
    line = port.line
    line.original_msisdn = line.original_msisdn or line.current_msisdn
    line.current_msisdn = port.portability_msisdn
    line.save(update_fields=["original_msisdn", "current_msisdn", "updated_at"])
    if line.sim_id:
        line.sim.msisdn = port.portability_msisdn
        line.sim.save(update_fields=["msisdn"])


def apply_status(port: PortRequest, new_status: str, *, port_date=None, source="webhook") -> bool:
    """Apply a supplier status. Returns False if it was stale or unknown."""
    new_status = (new_status or "").upper()
    if new_status not in S.values or new_status == S.AWAITING_ACTIVATION:
        return False
    with transaction.atomic():
        port = PortRequest.objects.select_for_update().get(pk=port.pk)
        if port.is_terminal:
            return False
        if new_status == port.status:
            if port_date and port_date != port.port_date:
                port.port_date = port_date
                port.save(update_fields=["port_date", "updated_at"])
                return True
            return False
        if (new_status in _PROGRESS and port.status in _PROGRESS
                and _PROGRESS.index(new_status) < _PROGRESS.index(port.status)):
            return False  # late or out-of-order event
        _set_status(port, new_status, port_date=port_date, source=source)
    return True
