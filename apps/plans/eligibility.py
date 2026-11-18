"""Who may buy a verified-only plan (student plans).

`can_purchase` is the single check used wherever a plan is priced on the
server (cart checkout, add-line and Switch & Save quotes). The approval rules
themselves live in each module's `services.get_status`.

Public sector workers don't have their own plans: they buy normal plans and
get a percentage off at checkout (apps.public_sector.services.offer_percent_for).
"""

from collections import defaultdict

from .models import Plan

NOT_ELIGIBLE_MESSAGES = {
    Plan.Audience.STUDENT: "This plan is only available to verified students.",
}


def audience_status(user, audience: str) -> str:
    """The user's verification status for a plan audience ("none" if unknown)."""
    if not user or not user.is_authenticated:
        return "none"
    # Imported here so the plans app doesn't depend on the modules at load time.
    if audience == Plan.Audience.STUDENT:
        from apps.student_discount.services import get_status
    else:
        return "none"
    return get_status(user)["status"]


def can_purchase(user, plan: Plan) -> bool:
    if plan.audience == Plan.Audience.GENERAL:
        return True
    return audience_status(user, plan.audience) == "approved"


def link_applications_by_email(model, user_model, open_statuses) -> tuple[int, int]:
    """Attach applications sent before login was required to their accounts.

    Matches the application's email to exactly one active account (ignoring
    case). An application is skipped when no account or several accounts
    match, or when it is open (pending/approved) and the account already has
    an open one, since each account may only have one open application.
    Newest applications are linked first.

    Works with both real and migration (historical) models. Returns
    (linked, skipped).
    """
    accounts = defaultdict(list)
    for pk, email in (
        user_model.objects.filter(is_active=True).exclude(email="").values_list("pk", "email")
    ):
        accounts[email.strip().lower()].append(pk)

    has_open = set(
        model.objects.filter(user__isnull=False, status__in=open_statuses)
        .values_list("user_id", flat=True)
    )

    linked = skipped = 0
    rows = (
        model.objects.filter(user__isnull=True)
        .only("pk", "email", "status")
        .order_by("-created_at", "-pk")
    )
    for row in rows:
        matches = accounts.get((row.email or "").strip().lower(), [])
        if len(matches) != 1:
            skipped += 1
            continue
        user_id = matches[0]
        if row.status in open_statuses:
            if user_id in has_open:
                skipped += 1
                continue
            has_open.add(user_id)
        model.objects.filter(pk=row.pk).update(user_id=user_id)
        linked += 1

    return linked, skipped
