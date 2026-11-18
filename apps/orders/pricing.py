"""Server-side pricing for the storefront checkout (create-intent).

The browser's cart is only used to know WHAT is being bought. Every price,
the coupon discount and the total are worked out here from the database:

* Plan lines (`id` = plan id): the price is picked by the line's `duration`
  in days: 730 → price_24, 365 → price_12, 30 → price_30, otherwise `price`
  (also when that term price is empty). Inactive or unknown plans are refused.
* Verified discounts (anyone can buy any active plan at its normal price):
  - an approved public sector worker gets the PublicSectorOffer % (30) off
    every plan covered by that offer, whichever page the plan came from;
  - an approved (not expired) student gets the StudentOffer % (20) off every
    student plan (Audience = Students).
  If both apply to a plan, the higher % wins (they are never added).
  Unit price = term price minus the %, rounded to the penny.
* Product lines (`type` "product", `id` = product variant id): the variant's price.
* Coupon (by code): applies to the whole cart, needs login, and its
  restrictions are enforced: status/dates/usage limit, assigned users,
  once per customer, and assigned plans (every line must be one of them).
  A coupon can't be combined with a public sector or student discount.

The caller compares our total with the one the customer saw and refuses
the payment if they differ (PRICE_CHANGED).
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db.models import Q

from apps.coupons.models import Coupon
from apps.coupons.services import validate_coupon
from apps.plans.eligibility import apply_discount
from apps.plans.models import Plan
from apps.products.models import ProductVariant
from apps.public_sector import services as public_sector
from apps.student_discount import services as student

PENNY = Decimal("0.01")
ZERO = Decimal("0.00")

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
    subtotal: Decimal          # sum of the lines (verified discounts already taken off)
    discount: Decimal          # coupon discount
    total: Decimal
    coupon: Coupon | None
    public_sector_discount: Decimal = ZERO   # how much the public sector offer saved
    student_discount: Decimal = ZERO         # how much the student offer saved

    def totals(self) -> dict:
        return {
            "subtotal": str(self.subtotal),
            "discount": str(self.discount),
            "public_sector_discount": str(self.public_sector_discount),
            "student_discount": str(self.student_discount),
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
                "list_price": f"{Decimal(str(line.get('list_price', line['price']))):.2f}",
                "discount_percent": line.get("discount_percent", "0.00"),
                "discount_programme": line.get("discount_programme"),
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

    verified_discount = _verified_discounts(buyer, plans.values())

    lines, cart_plans, has_products = [], [], False
    subtotal = ZERO
    saved = {"public_sector": ZERO, "student": ZERO}

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
            duration = _duration(item)
            list_price = plan_unit_price(plan, duration)
            percent, programme = verified_discount(plan)
            unit = apply_discount(list_price, percent) if percent else list_price
            if programme:
                saved[programme] += (list_price - unit) * quantity
            # The Transatel package comes from our plan, never from the browser.
            line = {
                **item,
                "type": "plan",
                "plan_id": plan.pk,
                "duration": duration,
                "transatelID": plan.transatelID or "",
                "list_price": float(list_price),
                "discount_percent": f"{percent:.2f}",
                "discount_programme": programme,
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

    if (coupon_code or "").strip() and any(saved.values()):
        label = "public sector" if saved["public_sector"] else "student"
        raise CheckoutError(
            "COUPON_NOT_COMBINABLE",
            f"Coupons can't be used together with your {label} discount.",
        )

    coupon, discount = _apply_coupon(coupon_code, buyer, subtotal, cart_plans, has_products)
    total = max(ZERO, subtotal - discount)

    return PricedCart(
        lines=lines, subtotal=subtotal, discount=discount, total=total, coupon=coupon,
        public_sector_discount=saved["public_sector"], student_discount=saved["student"],
    )


def _verified_discounts(buyer, plans):
    """Return f(plan) -> (percent, programme) for this buyer.

    Offers and approvals are looked up once per cart. Programme is
    "public_sector", "student" or None; the higher % wins if both apply.
    """
    if buyer is None:
        return lambda plan: (ZERO, None)

    plans = list(plans)

    ps_offer = public_sector.active_offer()
    ps_plan_ids = (
        set(public_sector.offer_plans(ps_offer).values_list("pk", flat=True)) if ps_offer else set()
    )
    ps_approved = bool(ps_plan_ids & {p.pk for p in plans}) and public_sector.is_approved(buyer)

    has_student_plans = any(p.audience == Plan.Audience.STUDENT for p in plans)
    st_offer = student.active_offer() if has_student_plans else None
    st_approved = st_offer is not None and student.is_approved(buyer)

    def discount_for(plan):
        options = []
        if ps_approved and plan.pk in ps_plan_ids:
            options.append((ps_offer.discount_percent, "public_sector"))
        if st_approved and plan.audience == Plan.Audience.STUDENT:
            options.append((st_offer.discount_percent, "student"))
        return max(options, key=lambda option: option[0], default=(ZERO, None))

    return discount_for


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
