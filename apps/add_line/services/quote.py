"""Price quote for one additional line (decisions Q4, A3, C11).

* Every active plan and every term can be bought (Q4). Term prices are per
  month and picked the same way as the storefront: 24M → price_24,
  12M → price_12, 30D → price_30, falling back to `price`.
* Prices are VAT-inclusive at the UK standard rate; VAT is shown, not added.
* Only coupons flagged `valid_for_additional_lines` apply (A3).
* Standard UK delivery is £0 ("Free UK delivery"); SIM / express fees are not
  published yet, so none are charged.

The quote is handed to the browser as a signed token. Checkout rebuilds the
quote server-side and refuses it if anything changed (QUOTE_CHANGED), so a
price can never come from the client.
"""

import calendar
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core import signing
from django.db.models import Q
from django.utils import timezone

from apps.coupons.models import Coupon
from apps.coupons.services import validate_coupon
from apps.plans.models import Plan

from ..models import SimType, Term

VAT_RATE = Decimal("0.20")
DELIVERY_FEE = Decimal("0.00")
CURRENCY = "GBP"

_TERM_PRICE_FIELD = {
    Term.DAYS_30: "price_30",
    Term.MONTHS_12: "price_12",
    Term.MONTHS_24: "price_24",
}
_COMMITMENT_MONTHS = {Term.DAYS_30: 0, Term.MONTHS_12: 12, Term.MONTHS_24: 24}

_TOKEN_SALT = "add_line.quote"
_PENNY = Decimal("0.01")


class QuoteError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Quote:
    plan: Plan
    term: str
    sim_type: str
    coupon: Coupon | None
    monthly_price: Decimal
    discount: Decimal
    first_renewal: date

    @property
    def charge_today(self) -> Decimal:
        return max(Decimal("0.00"), self.monthly_price - self.discount) + DELIVERY_FEE

    @property
    def vat_included(self) -> Decimal:
        net = self.charge_today / (1 + VAT_RATE)
        return (self.charge_today - net).quantize(_PENNY, rounding=ROUND_HALF_UP)

    def as_dict(self) -> dict:
        return {
            "plan": {
                "id": self.plan.pk,
                "name": self.plan.name,
                "data_allowance": self.plan.data_allowance or "",
            },
            "term": self.term,
            "term_label": Term(self.term).label,
            "commitment_months": _COMMITMENT_MONTHS[self.term],
            "sim_type": self.sim_type,
            "currency": CURRENCY,
            "monthly_price": str(self.monthly_price),
            "promo": (
                {"code": self.coupon.slug, "discount": str(self.discount)} if self.coupon else None
            ),
            "delivery_fee": str(DELIVERY_FEE),
            "charge_today": str(self.charge_today),
            "vat_included": str(self.vat_included),
            "vat_rate_percent": str(int(VAT_RATE * 100)),
            # Disclosed at checkout; how renewals are collected is not decided yet.
            "recurring_amount": str(self.monthly_price),
            "first_renewal_date": self.first_renewal.isoformat(),
        }

    @property
    def hash(self) -> str:
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def _add_month(d: date) -> date:
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _first_renewal(term: str, today: date) -> date:
    return today + timedelta(days=30) if term == Term.DAYS_30 else _add_month(today)


def _monthly_price(plan: Plan, term: str) -> Decimal:
    price = getattr(plan, _TERM_PRICE_FIELD[term])
    return Decimal(price if price is not None else plan.price).quantize(_PENNY)


def _resolve_coupon(code: str, user, plan: Plan, amount: Decimal, additional_line: bool = True):
    coupon = Coupon.objects.filter(Q(slug__iexact=code) | Q(name__iexact=code)).first()
    if not coupon:
        raise QuoteError("PROMO_NOT_FOUND", "We couldn't find that promo code.")
    if additional_line and not coupon.valid_for_additional_lines:
        raise QuoteError("PROMO_NOT_ELIGIBLE", "This promo code can't be used for an additional line.")
    valid, message, discount = validate_coupon(coupon, user, amount, plan)
    if not valid:
        raise QuoteError("PROMO_INVALID", message)
    return coupon, Decimal(str(discount)).quantize(_PENNY)


def build_quote(user, plan_id, term: str, sim_type: str, promo_code: str = "",
                *, additional_line: bool = True) -> Quote:
    """Price one line. `user` may be None for a guest preview (Switch & Save).

    `additional_line=False` is for a new customer's first line, where the
    "valid for additional lines" coupon restriction (A3) doesn't apply.
    """
    if term not in Term.values:
        raise QuoteError("TERM_INVALID", "Choose 30 days, 12 months or 24 months.")
    if sim_type not in SimType.values:
        raise QuoteError("SIM_TYPE_INVALID", "Choose eSIM or physical SIM.")

    plan = Plan.objects.filter(pk=plan_id, is_active=True).first()
    # A plan without a Transatel package code can't be activated.
    if not plan or not plan.transatelID:
        raise QuoteError("PLAN_NOT_AVAILABLE", "This plan isn't available right now.")

    monthly = _monthly_price(plan, term)
    coupon, discount = None, Decimal("0.00")
    if promo_code:
        coupon, discount = _resolve_coupon(promo_code.strip(), user, plan, monthly, additional_line)

    return Quote(
        plan=plan, term=term, sim_type=sim_type, coupon=coupon,
        monthly_price=monthly, discount=discount,
        first_renewal=_first_renewal(term, timezone.localdate()),
    )


def issue_token(user, quote: Quote) -> str:
    return signing.dumps(
        {
            "user": user.pk,
            "plan": quote.plan.pk,
            "term": quote.term,
            "sim_type": quote.sim_type,
            "promo": quote.coupon.slug if quote.coupon else "",
            "hash": quote.hash,
        },
        salt=_TOKEN_SALT,
    )


def redeem_token(user, token: str) -> Quote:
    """Rebuild the quote behind `token`, or raise QuoteError."""
    try:
        data = signing.loads(
            token, salt=_TOKEN_SALT, max_age=settings.ADD_LINE_QUOTE_MINUTES * 60
        )
    except signing.SignatureExpired:
        raise QuoteError("QUOTE_EXPIRED", "Your quote has expired. Please review the price again.")
    except signing.BadSignature:
        raise QuoteError("QUOTE_INVALID", "This quote isn't valid.")

    if data.get("user") != user.pk:
        raise QuoteError("QUOTE_INVALID", "This quote isn't valid.")

    quote = build_quote(user, data["plan"], data["term"], data["sim_type"], data["promo"])
    if quote.hash != data["hash"]:
        raise QuoteError("QUOTE_CHANGED", "The price has changed. Please review the new price.")
    return quote
