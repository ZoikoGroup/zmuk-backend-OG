"""Server-side pricing for the storefront checkout (create-intent).

The browser's cart is only used to know WHAT is being bought. Every price,
the coupon discount and the total are worked out here from the database:

* Plan lines (`id` = plan id): the price is picked by the line's `duration`
  in days: 730 → price_24, 365 → price_12, 30 → price_30, otherwise `price`
  (also when that term price is empty). Inactive or unknown plans are refused.
* Public sector / student plans: only for logged-in customers whose
  verification is approved (`apps.plans.eligibility.can_purchase`).
* Product lines (`type` "product", `id` = product variant id): the variant's price.
* Coupon (by code): applies to the whole cart, needs login, and its
  restrictions are enforced: status/dates/usage limit, assigned users,
  once per customer, and assigned plans (every line must be one of them).

The caller compares our total with the one the customer saw and refuses
the payment if they differ (PRICE_CHANGED).
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db.models import Q

from apps.coupons.models import Coupon
from apps.coupons.services import validate_coupon
from apps.plans.eligibility import NOT_ELIGIBLE_MESSAGES, can_purchase
from apps.plans.models import Plan
from apps.products.models import ProductVariant

PENNY = Decimal("0.01")

# Term (days, as the storefront sends it) → plan price field.
TERM_PRICE_FIELD = {730: "price_24", 365: "price_12", 30: "price_30"}


class CheckoutError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.extra = extra

    def as_dict(self) -> dict:
        # "error" is kept because the storefront already reads it.
        return {"error": self.message, "code": self.code, "message": self.message, **self.extra}


@dataclass
class PricedCart:
    lines: list
    subtotal: Decimal
    discount: Decimal
    total: Decimal
    coupon: Coupon | None

    def totals(self) -> dict:
        return {
            "subtotal": str(self.subtotal),
            "discount": str(self.discount),
            "shipping": "0.00",
            "total": str(self.total),
        }

    def summary_lines(self) -> list:
        """Short per-line view of our prices, returned with PRICE_CHANGED."""
        return [
            {
                "id": line.get("id"),
                "cartKey": line.get("cartKey"),
                "type": line["type"],
                "name": line.get("title") or line.get("name") or "",
                "quantity": line["quantity"],
                "unit_price": f"{Decimal(str(line['price'])):.2f}",
                "line_total": f"{Decimal(str(line['price'])) * line['quantity']:.2f}",
            }
            for line in self.lines
        ]


def money(value) -> Decimal:
    """Parse an amount to pence precision; raises (InvalidOperation, TypeError, ValueError)."""
    return Decimal(str(value)).quantize(PENNY, rounding=ROUND_HALF_UP)


def plan_unit_price(plan: Plan, duration_days) -> Decimal:
    field = TERM_PRICE_FIELD.get(duration_days)
    price = getattr(plan, field) if field else None
    return money(price if price is not None else plan.price)


def _raw(item: dict) -> dict:
    raw = item.get("_raw")
    return raw if isinstance(raw, dict) else {}


def _line_type(item: dict) -> str:
    # The checkout page keeps the cart item's type inside `_raw`.
    kind = item.get("type") or _raw(item).get("type") or "plan"
    return "product" if kind == "product" else "plan"


def _duration(item: dict):
    value = item.get("duration")
    if value in (None, ""):
        value = (_raw(item).get("metadata") or {}).get("duration")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_id(item: dict):
    try:
        return int(item.get("id"))
    except (TypeError, ValueError):
        return None


def _quantity(item: dict) -> int:
    try:
        quantity = int(item.get("quantity", 1))
    except (TypeError, ValueError):
        quantity = 0
    if quantity < 1:
        raise CheckoutError("CART_INVALID", "One of the items in your cart has an invalid quantity.")
    return quantity


def price_cart(cart, user, coupon_code: str = "") -> PricedCart:
    if not isinstance(cart, list) or not cart:
        raise CheckoutError("CART_EMPTY", "Your cart is empty.")
    if not all(isinstance(item, dict) for item in cart):
        raise CheckoutError("CART_INVALID", "Your cart couldn't be read. Please refresh the page.")

    buyer = user if user is not None and user.is_authenticated else None

    plan_ids = {_int_id(i) for i in cart if _line_type(i) == "plan"} - {None}
    variant_ids = {_int_id(i) for i in cart if _line_type(i) == "product"} - {None}
    plans = Plan.objects.filter(is_active=True).in_bulk(plan_ids)
    variants = (
        ProductVariant.objects.select_related("product")
        .filter(is_active=True, product__is_active=True)
        .in_bulk(variant_ids)
    )

    lines, cart_plans, has_products = [], [], False
    subtotal = Decimal("0.00")

    for item in cart:
        quantity = _quantity(item)
        name = item.get("title") or item.get("name") or "an item"

        if _line_type(item) == "product":
            variant = variants.get(_int_id(item))
            if variant is None:
                raise CheckoutError(
                    "PRODUCT_NOT_AVAILABLE", f"{name} is no longer available.", item=item.get("cartKey")
                )
            unit = money(variant.price)
            line = {**item, "type": "product", "variant_id": variant.pk}
            has_products = True
        else:
            plan = plans.get(_int_id(item))
            if plan is None:
                raise CheckoutError(
                    "PLAN_NOT_AVAILABLE", f"{name} is no longer available.", item=item.get("cartKey")
                )
            if not can_purchase(buyer, plan):
                if buyer is None:
                    raise CheckoutError(
                        "LOGIN_REQUIRED", "Please log in to buy this plan.", 401, item=item.get("cartKey")
                    )
                raise CheckoutError(
                    "PLAN_NOT_ELIGIBLE", NOT_ELIGIBLE_MESSAGES[plan.audience], 403, item=item.get("cartKey")
                )
            duration = _duration(item)
            unit = plan_unit_price(plan, duration)
            # The Transatel package comes from our plan, never from the browser.
            line = {
                **item,
                "type": "plan",
                "plan_id": plan.pk,
                "duration": duration,
                "transatelID": plan.transatelID or "",
            }
            cart_plans.append(plan)

        line_total = unit * quantity
        line.update({
            "quantity": quantity,
            "price": float(unit),
            "pricePerUnit": float(unit),
            "totalPrice": float(line_total),
        })
        lines.append(line)
        subtotal += line_total

    coupon, discount = _apply_coupon(coupon_code, buyer, subtotal, cart_plans, has_products)
    total = max(Decimal("0.00"), subtotal - discount)

    return PricedCart(lines=lines, subtotal=subtotal, discount=discount, total=total, coupon=coupon)


def _apply_coupon(code, buyer, subtotal: Decimal, cart_plans: list, has_products: bool):
    code = (code or "").strip()
    if not code:
        return None, Decimal("0.00")
    # Logged-in only, so "once per customer" can be enforced (the page already asks for login).
    if buyer is None:
        raise CheckoutError("LOGIN_REQUIRED", "Please log in to use a coupon.", 401)

    coupon = Coupon.objects.filter(Q(slug__iexact=code) | Q(name__iexact=code)).first()
    if coupon is None:
        raise CheckoutError("COUPON_INVALID", "We couldn't find that coupon.")

    plan = None
    allowed_plans = set(coupon.plans.values_list("pk", flat=True))
    if allowed_plans:
        # Whole-cart discount, so every item must be one of the coupon's plans.
        if has_products or not cart_plans or any(p.pk not in allowed_plans for p in cart_plans):
            raise CheckoutError("COUPON_INVALID", "This coupon isn't valid for the items in your cart.")
        plan = cart_plans[0]

    valid, message, discount = validate_coupon(coupon, buyer, subtotal, plan)
    if not valid:
        raise CheckoutError("COUPON_INVALID", message)
    return coupon, money(discount)
