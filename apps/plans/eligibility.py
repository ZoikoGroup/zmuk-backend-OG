"""Verified-customer discounts (public sector and students), shared helpers.

Anyone can buy any active plan at its normal price. Verified (approved)
customers get a percentage off at checkout:

* public sector workers: the PublicSectorOffer % on the normal plans it covers;
* students: the StudentOffer % on every student plan (audience "student",
  which are only listed on the student page).

Each module's `services` decides who is approved and which plans are covered;
the rounding and the `discounted_prices` shape live here so pages and
checkout always agree.
"""

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

_PENNY = Decimal("0.01")

TERM_PRICE_FIELDS = ("price_24", "price_12", "price_30", "price")


def apply_discount(price, percent) -> Decimal:
    """Price after `percent`% off, rounded to the nearest penny (half up)."""
    price = Decimal(str(price))
    return (price * (Decimal("100") - Decimal(str(percent))) / Decimal("100")).quantize(
        _PENNY, rounding=ROUND_HALF_UP
    )


def discounted_prices(plan_data: dict, percent):
    """`percent`% off each term price of a serialised plan (None stays None)."""
    return {
        field: (
            str(apply_discount(plan_data[field], percent))
            if plan_data.get(field) is not None
            else None
        )
        for field in TERM_PRICE_FIELDS
    }


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
