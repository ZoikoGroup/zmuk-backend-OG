"""Stripe payment outcomes for add-line orders.

`handle_payment_succeeded` is the single entry point for "paid": the Stripe
webhook and the browser's confirm call both use it, and it is idempotent.
A payment is a financial event only — it never marks a line active; that
waits for Transatel (see `fulfilment`).
"""

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.coupons.models import Coupon, CouponUsage
from core.stripe_compat import to_plain

from ..models import AddLineOrder
from . import audit, capacity

logger = logging.getLogger("apps.add_line")


class PaymentMismatch(Exception):
    """The PaymentIntent doesn't match the order it claims to pay for."""


def expected_livemode() -> bool:
    """Live keys take live payments only; test keys take test payments only."""
    key = getattr(settings, "STRIPE_SECRET_KEY", "") or ""
    return key.startswith(("sk_live_", "rk_live_"))


def _verify(order: AddLineOrder, intent: dict):
    pence = int(Decimal(order.total_gross) * 100)
    problems = []
    if intent.get("livemode") is not expected_livemode():
        problems.append("livemode")
    if intent.get("id") != order.stripe_payment_intent_id:
        problems.append("intent id")
    if (intent.get("metadata") or {}).get("add_line_order") != order.order_reference:
        problems.append("order reference")
    if intent.get("amount_received", intent.get("amount")) != pence:
        problems.append("amount")
    if (intent.get("currency") or "").lower() != "gbp":
        problems.append("currency")
    if intent.get("status") != "succeeded":
        problems.append("status")
    if problems:
        raise PaymentMismatch(f"{order.order_reference}: mismatched {', '.join(problems)}")


def handle_payment_succeeded(order: AddLineOrder, intent) -> AddLineOrder:
    """Mark the order paid (once), then try to fulfil it."""
    intent = to_plain(intent)
    _verify(order, intent)

    with transaction.atomic():
        order = AddLineOrder.objects.select_for_update().get(pk=order.pk)
        if order.payment_status == AddLineOrder.PaymentStatus.PAID:
            return order
        before = order.payment_status
        order.payment_status = AddLineOrder.PaymentStatus.PAID
        order.paid_at = timezone.now()
        order.save(update_fields=["payment_status", "paid_at", "updated_at"])
        if order.hold_id:
            capacity.mark_paid(order.hold)
        if order.coupon_id:
            _record_coupon_use(order)
        # Paid after the checkout had already expired (a narrow race with the
        # expiry job): the customer has paid, so it is honoured regardless.
        audit.record("payment_succeeded", order=order, before=before, after=order.payment_status,
                     payment_intent=order.stripe_payment_intent_id,
                     after_expiry=order.fulfilment_status == AddLineOrder.FulfilmentStatus.CANCELLED)
        if order.fulfilment_status == AddLineOrder.FulfilmentStatus.CANCELLED:
            order.fulfilment_status = AddLineOrder.FulfilmentStatus.NOT_STARTED
            order.save(update_fields=["fulfilment_status", "updated_at"])

    return _fulfil_after_payment(order)


def _fulfil_after_payment(order: AddLineOrder) -> AddLineOrder:
    """Paid is already committed, so an activation error must not surface as a
    payment error; retry_add_line_fulfilment picks the order up."""
    from .fulfilment import fulfil

    try:
        return fulfil(order)
    except Exception:
        logger.exception("Fulfilment after payment failed for %s; retry job will continue",
                         order.order_reference)
        order.refresh_from_db()
        return order


def record_payment_failure(order: AddLineOrder, intent) -> bool:
    """Note a declined attempt for support and the customer. The order and its
    line slot stay open: the customer can retry until the checkout hold expires."""
    intent = to_plain(intent)
    if intent.get("id") != order.stripe_payment_intent_id:
        raise PaymentMismatch(f"{order.order_reference}: mismatched intent id")
    error = intent.get("last_payment_error") or {}
    with transaction.atomic():
        order = AddLineOrder.objects.select_for_update().get(pk=order.pk)
        if order.payment_status != AddLineOrder.PaymentStatus.PENDING:
            return False
        # Stripe's code and customer-safe message only; never card details.
        order.last_payment_error = {
            key: error.get(key) for key in ("type", "code", "decline_code", "message") if error.get(key)
        }
        order.payment_failed_at = timezone.now()
        order.save(update_fields=["last_payment_error", "payment_failed_at", "updated_at"])
        audit.record("payment_failed", order=order, after=order.payment_status,
                     payment_intent=order.stripe_payment_intent_id,
                     code=error.get("code", ""), decline_code=error.get("decline_code", ""))
    return True


def mark_free_order_paid(order: AddLineOrder) -> AddLineOrder:
    """£0 orders (100% promo) skip Stripe entirely."""
    with transaction.atomic():
        order = AddLineOrder.objects.select_for_update().get(pk=order.pk)
        if order.payment_status != AddLineOrder.PaymentStatus.PAID:
            order.payment_status = AddLineOrder.PaymentStatus.PAID
            order.paid_at = timezone.now()
            order.save(update_fields=["payment_status", "paid_at", "updated_at"])
            if order.hold_id:
                capacity.mark_paid(order.hold)
            if order.coupon_id:
                _record_coupon_use(order)
            audit.record("payment_succeeded", order=order, after=order.payment_status, free=True)
    return _fulfil_after_payment(order)


def _record_coupon_use(order: AddLineOrder):
    CouponUsage.objects.create(coupon_id=order.coupon_id, user=order.owner, order_id=order.order_reference)
    Coupon.objects.filter(pk=order.coupon_id).update(used_count=F("used_count") + 1)
