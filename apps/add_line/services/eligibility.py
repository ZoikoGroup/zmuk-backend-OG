"""Can this customer add a line right now? (decisions Q3, A2, A4)

Only the account holder adds lines (Q3), so the authenticated user is the
subject — there are no delegated roles. Blocking reasons stop the journey;
`requires_reauth` does not block browsing plans, but checkout enforces it.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .capacity import CapacityUsage, get_usage

EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"
LINE_LIMIT_REACHED = "LINE_LIMIT_REACHED"

MESSAGES = {
    EMAIL_NOT_VERIFIED: "Please verify your email address before adding a new number.",
    LINE_LIMIT_REACHED: (
        "You've reached the maximum number of lines for one account. "
        "Contact support if you need more."
    ),
}


@dataclass
class Eligibility:
    usage: CapacityUsage
    requires_reauth: bool
    reasons: list = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return not self.reasons


def login_is_recent(user) -> bool:
    if not user.last_login:
        return False
    max_age = timedelta(minutes=settings.ADD_LINE_REAUTH_MINUTES)
    return timezone.now() - user.last_login <= max_age


def check(user) -> Eligibility:
    reasons = []
    # Accounts stay inactive until the email link is clicked; Google sign-in
    # only creates active users for verified Google emails.
    if not (user.is_active and user.email):
        reasons.append(EMAIL_NOT_VERIFIED)

    usage = get_usage(user)
    if usage.remaining <= 0:
        reasons.append(LINE_LIMIT_REACHED)

    return Eligibility(usage=usage, requires_reauth=not login_is_recent(user), reasons=reasons)
