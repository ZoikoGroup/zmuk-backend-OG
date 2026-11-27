"""One line-changing operation per line at a time (CTO response §4.1).

`acquire` must run inside a transaction. It locks the `Line` row first, so
two requests for the same line queue up instead of racing, then refuses if:

* another operation already holds the line's lock, or
* the line has a number transfer (PAC/STAC) still in progress.

The partial unique constraint on `LineOperationLock` is the final guard if
anything slips past the checks.

Number transfers keep their own one-open-request rule in add_line; they are
checked here so a SIM change never starts mid-port.
"""

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.add_line.models import Line, PortRequest

from ..models import LineOperationLock


class LineBusy(Exception):
    """The line is in the middle of another operation."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def lock_line_row(line_id) -> Line:
    """SELECT … FOR UPDATE on the line (no-op lock on SQLite)."""
    return Line.objects.select_for_update().get(pk=line_id)


def current_lock(line) -> LineOperationLock | None:
    return LineOperationLock.objects.filter(line=line, released_at__isnull=True).first()


def open_port_request(line) -> PortRequest | None:
    return line.port_requests.exclude(status__in=PortRequest.TERMINAL_STATUSES).first()


def acquire(line, operation: str, reference: str) -> LineOperationLock:
    """Take the line's lock for `operation`. Raises LineBusy if it's taken."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("line_lock.acquire must run inside transaction.atomic()")

    line = lock_line_row(line.pk)
    held = current_lock(line)
    if held is not None:
        if held.operation == operation and held.reference == str(reference):
            return held  # same holder asking again (idempotent)
        raise LineBusy("LINE_OPERATION_IN_PROGRESS", "Another change is already in progress on this line.")
    if open_port_request(line) is not None:
        raise LineBusy("PORT_IN_PROGRESS", "A number transfer is in progress on this line.")

    try:
        with transaction.atomic():
            return LineOperationLock.objects.create(line=line, operation=operation, reference=str(reference))
    except IntegrityError as exc:
        raise LineBusy("LINE_OPERATION_IN_PROGRESS", "Another change is already in progress on this line.") from exc


def release(line, reference: str, reason: str = "") -> bool:
    """Release the lock held under `reference`. Returns False if none was held."""
    updated = LineOperationLock.objects.filter(
        line=line, reference=str(reference), released_at__isnull=True,
    ).update(released_at=timezone.now(), release_reason=reason[:64])
    return bool(updated)
