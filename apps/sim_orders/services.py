"""Stripe integration for SIM purchases (same pattern as the recharge app).

Stripe Checkout hosts the card page -> PCI SAQ A + UK 3-D Secure handled for us.
Amounts in pence. The webhook is the source of truth for 'paid'.
"""
import stripe
from django.conf import settings


class StripeNotConfigured(Exception):
    pass


def _init():
    key = getattr(settings, "STRIPE_SECRET_KEY", "") or ""
    if not key:
        raise StripeNotConfigured("STRIPE_SECRET_KEY is not set.")
    stripe.api_key = key


def create_checkout_session(order, success_url: str, cancel_url: str):
    _init()
    return stripe.checkout.Session.create(
        mode="payment",
        customer_email=order.email or None,
        line_items=[
            {
                "price_data": {
                    "currency": order.currency,
                    "unit_amount": order.amount_pence,
                    "product_data": {"name": order.product.name},
                },
                "quantity": 1,
            }
        ],
        metadata={"order_ref": order.order_ref, "kind": "sim_purchase"},
        success_url=f"{success_url}?ref={order.order_ref}",
        cancel_url=f"{cancel_url}?ref={order.order_ref}",
        idempotency_key=f"sim-checkout-{order.order_ref}",
    )


def construct_webhook_event(payload: bytes, sig_header: str):
    secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "") or ""
    if not secret:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET is not set.")
    return stripe.Webhook.construct_event(payload, sig_header, secret)
