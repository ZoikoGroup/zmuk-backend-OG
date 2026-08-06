"""Stripe integration for recharge payments.

Uses Stripe Checkout (Stripe hosts the card page -> keeps us at PCI SAQ A and
handles UK Strong Customer Authentication / 3-D Secure automatically).
Amounts are integers in pence. The webhook is the source of truth for 'paid'.
"""
import stripe
from django.conf import settings


class StripeNotConfigured(Exception):
    pass


def _init():
    key = getattr(settings, "STRIPE_SECRET_KEY", "") or ""
    if not key:
        raise StripeNotConfigured(
            "STRIPE_SECRET_KEY is not set. Add your Stripe test secret key to the "
            "environment / settings before taking payments."
        )
    stripe.api_key = key


def create_checkout_session(order, success_url: str, cancel_url: str):
    """Create a Stripe Checkout Session for a RechargeOrder. Returns the session."""
    _init()
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": order.currency,
                    "unit_amount": order.amount_pence,  # pence
                    "product_data": {
                        "name": f"{order.get_module_display()} — {order.msisdn}",
                    },
                },
                "quantity": 1,
            }
        ],
        metadata={
            "order_ref": order.order_ref,
            "module": order.module,
            "msisdn": order.msisdn,
        },
        success_url=f"{success_url}?ref={order.order_ref}",
        cancel_url=f"{cancel_url}?ref={order.order_ref}",
        # Idempotency: retrying the same order never creates a second session/charge.
        idempotency_key=f"recharge-checkout-{order.order_ref}",
    )
    return session


def construct_webhook_event(payload: bytes, sig_header: str):
    """Verify the webhook signature and return the Stripe event (raises on tamper)."""
    secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "") or ""
    if not secret:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET is not set.")
    return stripe.Webhook.construct_event(payload, sig_header, secret)
