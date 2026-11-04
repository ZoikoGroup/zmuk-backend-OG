"""Per-account line cap and slot holds (decision A2).

A slot is used by every line that is pending activation, active or suspended,
plus every HELD `CapacityHold` that has not expired. A hold with
`expires_at=None` is a paid order waiting for fulfilment and never expires.

`acquire_hold` locks the user's row before counting, so two checkouts running
at the same time for the same account cannot both take the last slot.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import CapacityHold, Line, LineCapOverride


class CapacityExceeded(Exception):
    def __init__(self, usage: "CapacityUsage"):
        super().__init__(f"Line limit reached ({usage.used}/{usage.limit}).")
        self.usage = usage


@dataclass(frozen=True)
class CapacityUsage:
    lines: int
    holds: int
    limit: int

    @property
    def used(self) -> int:
        return self.lines + self.holds

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def line_limit(user) -> int:
    override = LineCapOverride.objects.filter(owner=user).only("max_lines").first()
    return override.max_lines if override else settings.MAX_CONSUMER_LINES


def _active_holds(user):
    now = timezone.now()
    return CapacityHold.objects.filter(owner=user, status=CapacityHold.Status.HELD).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )


def get_usage(user) -> CapacityUsage:
    return CapacityUsage(
        lines=Line.objects.filter(owner=user, status__in=Line.CAPACITY_STATUSES).count(),
        holds=_active_holds(user).count(),
        limit=line_limit(user),
    )


def acquire_hold(user) -> CapacityHold:
    """Take one slot for a checkout, or raise `CapacityExceeded`."""
    with transaction.atomic():
        # Serialises concurrent checkouts for this account (row lock on
        # Postgres; SQLite already serialises writers).
        get_user_model().objects.select_for_update().only("pk").get(pk=user.pk)
        usage = get_usage(user)
        if usage.remaining <= 0:
            raise CapacityExceeded(usage)
        return CapacityHold.objects.create(
            owner=user,
            expires_at=timezone.now() + timedelta(minutes=settings.LINE_HOLD_MINUTES),
        )


def mark_paid(hold: CapacityHold) -> None:
    """Payment succeeded: keep the slot until fulfilment, without expiry."""
    CapacityHold.objects.filter(pk=hold.pk, status=CapacityHold.Status.HELD).update(
        expires_at=None, updated_at=timezone.now()
    )


def consume_hold(hold: CapacityHold) -> None:
    """The line now exists and counts by itself; retire the hold."""
    CapacityHold.objects.filter(pk=hold.pk, status=CapacityHold.Status.HELD).update(
        status=CapacityHold.Status.CONSUMED, updated_at=timezone.now()
    )


def release_hold(hold: CapacityHold, reason: str) -> None:
    CapacityHold.objects.filter(pk=hold.pk, status=CapacityHold.Status.HELD).update(
        status=CapacityHold.Status.RELEASED, release_reason=reason[:64], updated_at=timezone.now()
    )


def release_expired() -> int:
    """Release unpaid holds past their expiry. Returns how many were released."""
    return CapacityHold.objects.filter(
        status=CapacityHold.Status.HELD, expires_at__lte=timezone.now()
    ).update(
        status=CapacityHold.Status.RELEASED, release_reason="expired", updated_at=timezone.now()
    )
