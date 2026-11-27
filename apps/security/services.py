"""Read side of account security events, for risk checks."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.conf import settings
from django.utils import timezone

from .models import AccountSecurityEvent

HISTORY_COMPLETE = "COMPLETE"
UNKNOWN_PRE_TRACKING = "UNKNOWN_PRE_TRACKING"


@dataclass
class RecentChanges:
    kinds: set = field(default_factory=set)
    # False when the window reaches back before recording started, so an
    # empty result does not prove nothing changed (clarification B3).
    history_complete: bool = True

    def changed(self, kind) -> bool:
        return kind in self.kinds

    @property
    def history_status(self) -> str:
        """Addendum v2.1 B3 wording: shown to the agent, never an automatic hold."""
        return HISTORY_COMPLETE if self.history_complete else UNKNOWN_PRE_TRACKING


def tracking_started() -> date:
    raw = getattr(settings, "ACCOUNT_SECURITY_EVENTS_SINCE", "") or ""
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return timezone.localdate()


def recent_changes(user, days: int, *, now: datetime | None = None) -> RecentChanges:
    now = now or timezone.now()
    since = now - timedelta(days=days)
    kinds = set(
        AccountSecurityEvent.objects.filter(user=user, occurred_at__gte=since).values_list("kind", flat=True)
    )
    return RecentChanges(kinds=kinds, history_complete=timezone.localdate(since) >= tracking_started())
