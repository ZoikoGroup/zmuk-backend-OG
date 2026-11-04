"""Checkout for an additional line: hold a slot and a SIM, then take payment.

Order of operations in `start_checkout`:

    1. eligibility + recent login (A4), signed quote rebuilt server-side;
    2. hold a line slot (A2) and reserve one SIM of the chosen type;
    3. store the contract acceptance (C12) and create the Stripe PaymentIntent.

If any step after the hold fails, everything taken so far is given back.
Payment success is handled by `payments.handle_payment_succeeded`, called from
either the Stripe webhook or the browser's confirm call — whichever lands first.
"""

import logging
from decimal import Decimal

import stripe
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.sims import reservations
from core.stripe_compat import to_plain

from ..models import AddLineOrder, CapacityHold, ContractAcceptance, SimType
from . import audit, capacity, eligibility
from .quote import QuoteError, redeem_token

logger = logging.getLogger("apps.add_line")

STRIPE_SOURCE = "zoiko_add_line"
# Stripe's minimum charge in GBP.
_STRIPE_MIN_CHARGE = Decimal("0.30")


class CheckoutError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def cart_key(order: AddLineOrder) -> str:
    return f"addline-{order.order_reference}"


def stripe_configured() -> bool:
    key = getattr(settings, "STRIPE_SECRET_KEY", "") or ""
    if key:
        stripe.api_key = key
    return bool(key)


def _check_customer(user):
    result = eligibility.check(user)
    if eligibility.EMAIL_NOT_VERIFIED in result.reasons:
        raise CheckoutError(
            eligibility.EMAIL_NOT_VERIFIED, eligibility.MESSAGES[eligibility.EMAIL_NOT_VERIFIED], 403
        )
    if result.requires_reauth:
        raise CheckoutError("REAUTH_REQUIRED", "Please log in again to continue.", 403)


def _check_contract(contract: dict):
    if (
        contract.get("contract_version") != settings.ADD_LINE_CONTRACT_VERSION
        or contract.get("contract_summary_version") != settings.ADD_LINE_CONTRACT_SUMMARY_VERSION
    ):
        raise CheckoutError(
            "CONTRACT_OUTDATED", "Our terms have been updated. Please review them again.", 409
        )


def release(order: AddLineOrder, reason: str, actor=None):
    """Give back the slot and SIM held for an unpaid order and close it."""
    if order.hold_id:
        capacity.release_hold(order.hold, reason)
    reservations.release_cart(cart_key(order))
    before = order.fulfilment_status
    order.fulfilment_status = AddLineOrder.FulfilmentStatus.CANCELLED
    if order.payment_status == AddLineOrder.PaymentStatus.PENDING:
        order.payment_status = AddLineOrder.PaymentStatus.FAILED
    order.save(update_fields=["fulfilment_status", "payment_status", "updated_at"])
    audit.record("checkout_released", order=order, actor=actor, before=before,
                 after=order.fulfilment_status, reason=reason)


def start_checkout(user, *, quote_token, switch_choice, billing, delivery_address,
                   contract, ip_address=None, user_agent=""):
    """Returns (order, client_secret). client_secret is None for a £0 order."""
    _check_customer(user)
    try:
        quote = redeem_token(user, quote_token)
    except QuoteError as exc:
        raise CheckoutError(exc.code, exc.message, 409 if exc.code == "QUOTE_CHANGED" else 400)

    _check_contract(contract)
    if quote.sim_type == SimType.PSIM and not delivery_address:
        raise CheckoutError("DELIVERY_ADDRESS_REQUIRED", "Enter a delivery address for your SIM.")

    amount = quote.charge_today
    if Decimal("0") < amount < _STRIPE_MIN_CHARGE:
        raise CheckoutError("AMOUNT_TOO_LOW", "This order total can't be charged. Please remove the promo code.")
    needs_payment = amount > 0
    if needs_payment and not stripe_configured():
        raise CheckoutError("PAYMENTS_UNAVAILABLE", "Payments are not available right now.", 503)

    try:
        hold = capacity.acquire_hold(user)
    except capacity.CapacityExceeded:
        raise CheckoutError(
            eligibility.LINE_LIMIT_REACHED, eligibility.MESSAGES[eligibility.LINE_LIMIT_REACHED], 409
        )

    order = AddLineOrder.objects.create(
        owner=user, hold=hold, plan=quote.plan, term=quote.term, sim_type=quote.sim_type,
        switch_choice=switch_choice, quote=quote.as_dict(), quote_hash=quote.hash,
        coupon=quote.coupon, total_gross=amount, vat_amount=quote.vat_included,
        billing={**billing, "email": billing.get("email") or user.email},
        delivery_address=delivery_address if quote.sim_type == SimType.PSIM else None,
    )
    audit.record("checkout_started", order=order, actor=user,
                 after=order.payment_status, amount=str(amount))

    try:
        sim_ids = reservations.reserve_sims(
            quote.sim_type, 1, cart_key(order), session_id=order.order_reference
        )
    except reservations.InsufficientInventory:
        release(order, "out_of_stock", actor=user)
        raise CheckoutError("OUT_OF_STOCK", "This SIM type is out of stock right now. Please try again later.", 409)

    order.reserved_sim_id = sim_ids[0]
    order.fulfilment_status = AddLineOrder.FulfilmentStatus.SIM_RESERVED
    order.save(update_fields=["reserved_sim", "fulfilment_status", "updated_at"])

    now = timezone.now()
    ContractAcceptance.objects.create(
        order=order,
        contract_version=contract["contract_version"],
        contract_summary_version=contract["contract_summary_version"],
        documents_hash=contract.get("documents_hash", ""),
        accepted_at=now,
        immediate_service_requested_at=now if contract.get("immediate_service_requested") else None,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:255],
    )

    if not needs_payment:
        return order, None

    try:
        intent = stripe.PaymentIntent.create(
            amount=int(amount * 100),
            currency="gbp",
            automatic_payment_methods={"enabled": True},
            receipt_email=order.billing.get("email") or None,
            metadata={"add_line_order": order.order_reference, "source": STRIPE_SOURCE},
            idempotency_key=f"add-line-intent-{order.order_reference}",
        )
    except Exception:
        logger.exception("Add-line PaymentIntent creation failed for %s", order.order_reference)
        release(order, "payment_init_failed", actor=user)
        raise CheckoutError("PAYMENT_INIT_FAILED", "We couldn't start the payment. Please try again.", 502)

    order.stripe_payment_intent_id = intent.id
    order.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    return order, intent.client_secret


def expire_abandoned() -> int:
    """Close unpaid checkouts whose slot hold has run out.

    Cancels the PaymentIntent first so a late payment can't land on a released
    slot. If Stripe says it already succeeded, the payment is honoured instead.
    Returns the number of orders closed.
    """
    from .payments import handle_payment_succeeded

    now = timezone.now()
    stale = AddLineOrder.objects.filter(
        payment_status=AddLineOrder.PaymentStatus.PENDING,
        hold__status=CapacityHold.Status.HELD,
        hold__expires_at__lte=now,
    ).select_related("hold")

    closed = 0
    for order in stale:
        if order.stripe_payment_intent_id:
            if not stripe_configured():
                continue
            try:
                stripe.PaymentIntent.cancel(order.stripe_payment_intent_id)
            except Exception:
                # Cancel refused or failed: find out why before releasing,
                # since the customer may have just paid.
                try:
                    intent = to_plain(stripe.PaymentIntent.retrieve(order.stripe_payment_intent_id))
                except Exception:
                    logger.warning("Could not check PaymentIntent for %s; retrying next run",
                                   order.order_reference, exc_info=True)
                    continue
                if intent.get("status") == "succeeded":
                    handle_payment_succeeded(order, intent)
                    continue
                if intent.get("status") != "canceled":
                    logger.warning("PaymentIntent for %s not cancellable (status %s); retrying next run",
                                   order.order_reference, intent.get("status"))
                    continue
        with transaction.atomic():
            release(order, "checkout_expired")
        closed += 1
    return closed
