"""Stripe integration for recharge payments.

Uses Stripe Checkout (hosted card page → PCI SAQ A, automatic SCA/3DS).
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
    """Create a Stripe Checkout Session for a RechargeOrder."""
    _init()

    product_name = f"{order.get_module_display()} — {order.msisdn}"
    if order.product:
        product_name = f"{order.product.name} — {order.msisdn}"

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": order.currency,
                    "unit_amount": order.amount_pence,
                    "product_data": {"name": product_name},
                },
                "quantity": 1,
            }
        ],
        metadata={
            "order_ref": order.order_ref,
            "module": order.module,
            "msisdn": order.msisdn,
            "sim_serial": order.sim_serial or "",
        },
        success_url=f"{success_url}?ref={order.order_ref}",
        cancel_url=f"{cancel_url}?ref={order.order_ref}",
        idempotency_key=f"recharge-checkout-{order.order_ref}",
    )
    return session


def construct_webhook_event(payload: bytes, sig_header: str):
    """Verify webhook signature and return the Stripe event."""
    secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "") or ""
    if not secret:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET is not set.")
    return stripe.Webhook.construct_event(payload, sig_header, secret)


# ── Inline payment (PaymentIntent) — matches the WordPress modal flow ─────
# The WP plugin charges inside its own modal via Stripe Elements rather than
# redirecting to a hosted page. These helpers give the Django API the same
# capability: create an intent, hand the client_secret to the browser, then
# verify server-side before touching Transatel.

def create_payment_intent(order, customer_email: str = ""):
    """Create a Stripe PaymentIntent for inline (on-page) payment.

    Returns the PaymentIntent object. The caller must pass
    `payment_intent.client_secret` to the browser, where Stripe.js confirms
    the card / Google Pay / Apple Pay payment without leaving the site.
    """
    _init()

    product_name = f"{order.get_module_display()} — {order.msisdn}"
    if order.product:
        product_name = f"{order.product.name} — {order.msisdn}"

    kwargs = {
        "amount": order.amount_pence,
        "currency": order.currency,
        "description": product_name,
        "automatic_payment_methods": {"enabled": True},
        "metadata": {
            "order_ref": order.order_ref,
            "module": order.module,
            "msisdn": order.msisdn,
            "sim_serial": order.sim_serial or "",
        },
    }
    email = (customer_email or order.customer_email or "").strip()
    if email:
        kwargs["receipt_email"] = email

    return stripe.PaymentIntent.create(
        **kwargs,
        idempotency_key=f"recharge-intent-{order.order_ref}",
    )


def retrieve_payment_intent(payment_intent_id: str):
    """Fetch a PaymentIntent from Stripe so we can verify it server-side.

    Never trust the browser's word that a payment succeeded — always confirm
    against Stripe before reactivating a SIM.
    """
    _init()
    return stripe.PaymentIntent.retrieve(payment_intent_id)
