"""Saved payment methods: the only code that talks to Stripe about saved cards.

Cards are entered in Stripe's own fields; this module only ever sees Stripe
IDs and display details (brand, last 4, expiry). Every saved card is set up
for payments the customer makes themselves ("on_session"). Nothing here
charges a card: checkout creates the PaymentIntent and the customer confirms
it in the browser.

A card can be saved three ways, all finished by `_store` under a lock so
retries, the browser and the webhook never create duplicates:
  * dashboard "Add a card" (SetupIntent)    → finish_setup()
  * "Save this card" at checkout / add-line  → save_from_payment()
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from core.stripe_compat import to_plain

from .models import PaymentMethod, PaymentMethodConsent, PaymentMethodSetup, StripeCustomer

logger = logging.getLogger("apps.payment_methods")

STRIPE_SOURCE = "zoiko_payment_methods"

# Approved wording (decision note §5.1). Changing the text needs a new version.
SAVE_CARD_CONSENT_VERSION = "save-card-v1"
SAVE_CARD_CONSENT_TEXT = "Save this card for payments I choose to make in MyZoiko or at checkout."


class PaymentMethodError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def enabled() -> bool:
    return bool(getattr(settings, "SAVED_PAYMENT_METHODS_ENABLED", False))


def max_methods() -> int:
    return int(getattr(settings, "SAVED_PAYMENT_METHODS_MAX", 3))


def expected_livemode() -> bool:
    """Live keys use live Customers only; test keys use test Customers only."""
    key = getattr(settings, "STRIPE_SECRET_KEY", "") or ""
    return key.startswith(("sk_live_", "rk_live_"))


def _stripe_ready():
    key = getattr(settings, "STRIPE_SECRET_KEY", "") or ""
    if not key:
        raise PaymentMethodError("PAYMENTS_UNAVAILABLE", "Payments are not available right now.", 503)
    stripe.api_key = key


def active_methods(user):
    return PaymentMethod.objects.filter(
        user=user, status=PaymentMethod.Status.ACTIVE, customer__livemode=expected_livemode(),
    )


def get_active(user, method_id) -> PaymentMethod:
    """The user's own active method, or PAYMENT_METHOD_NOT_FOUND (also for
    another account's method, so IDs can't be probed)."""
    try:
        method_id = uuid.UUID(str(method_id))
    except (TypeError, ValueError):
        method_id = None
    method = active_methods(user).select_related("customer").filter(pk=method_id).first() if method_id else None
    if not method:
        raise PaymentMethodError("PAYMENT_METHOD_NOT_FOUND", "We couldn't find that saved card.", 404)
    return method


def _check_room(user):
    if active_methods(user).count() >= max_methods():
        raise PaymentMethodError(
            "LIMIT_REACHED",
            f"You can save up to {max_methods()} cards. Remove one to add another.",
            409,
        )


def _check_consent(consent_version):
    if consent_version != SAVE_CARD_CONSENT_VERSION:
        raise PaymentMethodError(
            "CONSENT_OUTDATED", "Please review and accept the latest wording to save your card.", 409,
        )


def _consent(user, *, accepted_at, ip_address, user_agent) -> PaymentMethodConsent:
    return PaymentMethodConsent.objects.create(
        user=user,
        kind=PaymentMethodConsent.Kind.SAVE_FOR_MY_PAYMENTS,
        wording_version=SAVE_CARD_CONSENT_VERSION,
        wording_text=SAVE_CARD_CONSENT_TEXT,
        accepted_at=accepted_at,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:255],
    )


# ── Stripe Customer ─────────────────────────────────────────────────────

def get_or_create_customer(user) -> StripeCustomer:
    live = expected_livemode()
    existing = StripeCustomer.objects.filter(user=user, livemode=live).first()
    if existing:
        return existing
    _stripe_ready()
    with transaction.atomic():
        # Lock the user row so two first-time requests can't make two Customers.
        get_user_model().objects.select_for_update().filter(pk=user.pk).first()
        existing = StripeCustomer.objects.filter(user=user, livemode=live).first()
        if existing:
            return existing
        try:
            customer = stripe.Customer.create(
                email=user.email or None,
                metadata={"user_id": str(user.pk)},
                idempotency_key=f"zoiko-customer-{user.pk}-{'live' if live else 'test'}",
            )
        except Exception:
            logger.exception("Stripe Customer creation failed for user %s", user.pk)
            raise PaymentMethodError("PAYMENTS_UNAVAILABLE", "We couldn't set up saved cards right now.", 502)
        return StripeCustomer.objects.create(user=user, stripe_customer_id=customer.id, livemode=live)


# ── Dashboard: add a card (SetupIntent, no payment taken) ────────────────

def start_setup(user, *, consent_version, ip_address=None, user_agent="") -> str:
    """Returns the SetupIntent client secret for Stripe's card form."""
    _check_consent(consent_version)
    _check_room(user)
    accepted_at = timezone.now()
    customer = get_or_create_customer(user)
    try:
        intent = stripe.SetupIntent.create(
            customer=customer.stripe_customer_id,
            usage="on_session",
            payment_method_types=["card"],
            metadata={"source": STRIPE_SOURCE, "user_id": str(user.pk)},
        )
    except Exception:
        logger.exception("SetupIntent creation failed for user %s", user.pk)
        raise PaymentMethodError("SETUP_INIT_FAILED", "We couldn't start adding your card. Please try again.", 502)

    with transaction.atomic():
        consent = _consent(user, accepted_at=accepted_at, ip_address=ip_address, user_agent=user_agent)
        PaymentMethodSetup.objects.create(
            stripe_intent_id=intent.id, user=user, customer=customer, consent=consent,
            source=PaymentMethodSetup.Source.DASHBOARD,
        )
    return intent.client_secret


def confirm_setup(user, setup_intent_id) -> PaymentMethodSetup:
    """Browser confirm after stripe.confirmSetup(): ask Stripe, never the browser."""
    setup = PaymentMethodSetup.objects.filter(
        stripe_intent_id=setup_intent_id or "", user=user, source=PaymentMethodSetup.Source.DASHBOARD,
    ).first()
    if not setup:
        raise PaymentMethodError("SETUP_NOT_FOUND", "We couldn't find that card setup.", 404)
    if setup.finished:
        return setup
    _stripe_ready()
    try:
        intent = stripe.SetupIntent.retrieve(setup.stripe_intent_id)
    except Exception:
        logger.exception("Could not retrieve SetupIntent for setup %s", setup.pk)
        raise PaymentMethodError("SETUP_CHECK_FAILED", "We couldn't check your card yet. Please try again.", 502)
    return finish_setup(intent, user=user)


def finish_setup(intent, *, user=None) -> PaymentMethodSetup:
    """Save the card from a succeeded SetupIntent. Called by the browser's
    confirm (with `user`) and by the webhook (without). Idempotent."""
    intent = to_plain(intent)
    with transaction.atomic():
        setup = (
            PaymentMethodSetup.objects.select_for_update()
            .select_related("customer", "consent")
            .filter(stripe_intent_id=intent.get("id"))
            .first()
        )
        if not setup or (user is not None and setup.user_id != user.pk):
            raise PaymentMethodError("SETUP_NOT_FOUND", "We couldn't find that card setup.", 404)
        if setup.finished:
            return setup
        _verify_intent(setup, intent)
        if intent.get("status") != "succeeded":
            raise PaymentMethodError(
                "SETUP_NOT_COMPLETED", "Your card hasn't been confirmed yet. No payment has been taken.", 402,
            )
        _store(setup, _payment_method_details(intent.get("payment_method")))
    return setup


# ── Checkout: pay with a saved card, or save the new card ────────────────

@dataclass
class CheckoutPlan:
    """What a checkout adds to its PaymentIntent for saved cards."""

    intent_params: dict = field(default_factory=dict)
    payment_method: PaymentMethod | None = None
    # Set when the customer ticked "save this card".
    save_customer: StripeCustomer | None = None
    accepted_at: object = None


def plan_checkout(user, *, payment_method_id=None, save_card=False, consent_version=None):
    """None when the checkout uses neither feature (the flow is then unchanged)."""
    if not payment_method_id and not save_card:
        return None
    if not enabled():
        raise PaymentMethodError("SAVED_CARDS_UNAVAILABLE", "Saved cards aren't available yet.", 400)
    if not user or not user.is_authenticated:
        raise PaymentMethodError("LOGIN_REQUIRED", "Please log in to use saved cards.", 401)
    if payment_method_id and save_card:
        raise PaymentMethodError("INVALID_REQUEST", "Choose a saved card or save a new one, not both.", 400)

    if payment_method_id:
        method = get_active(user, payment_method_id)
        return CheckoutPlan(
            intent_params={
                "customer": method.customer.stripe_customer_id,
                "payment_method": method.stripe_payment_method_id,
                "payment_method_types": ["card"],
            },
            payment_method=method,
        )

    _check_consent(consent_version)
    _check_room(user)
    customer = get_or_create_customer(user)
    return CheckoutPlan(
        intent_params={
            "customer": customer.stripe_customer_id,
            "setup_future_usage": "on_session",
            "payment_method_types": ["card"],
        },
        save_customer=customer,
        accepted_at=timezone.now(),
    )


def apply_plan(intent_kwargs: dict, plan) -> dict:
    """Merge a CheckoutPlan into PaymentIntent.create kwargs. Stripe refuses
    automatic_payment_methods together with payment_method_types."""
    if not plan:
        return intent_kwargs
    kwargs = {k: v for k, v in intent_kwargs.items() if k != "automatic_payment_methods"}
    kwargs.update(plan.intent_params)
    return kwargs


def record_checkout_save(user, plan, intent_id, *, source, ip_address=None, user_agent=""):
    """After the PaymentIntent exists: remember to save its card once paid."""
    if not plan or not plan.save_customer:
        return
    with transaction.atomic():
        consent = _consent(user, accepted_at=plan.accepted_at, ip_address=ip_address, user_agent=user_agent)
        PaymentMethodSetup.objects.create(
            stripe_intent_id=intent_id, user=user, customer=plan.save_customer, consent=consent, source=source,
        )


def save_from_payment(intent):
    """Save the card of a succeeded checkout payment when the customer asked
    to. No-op for any other payment. Idempotent."""
    intent = to_plain(intent)
    if intent.get("status") != "succeeded":
        return None
    with transaction.atomic():
        setup = (
            PaymentMethodSetup.objects.select_for_update()
            .select_related("customer", "consent")
            .filter(stripe_intent_id=intent.get("id"))
            .first()
        )
        if not setup or setup.finished:
            return setup
        _verify_intent(setup, intent)
        _store(setup, _payment_method_details(intent.get("payment_method")))
    return setup


def save_from_payment_safely(intent):
    """For payment handlers: the order is already paid, so a saving problem
    is logged, never raised."""
    try:
        return save_from_payment(intent)
    except Exception:
        logger.exception("Could not save card from payment %s", (to_plain(intent) or {}).get("id"))
        return None


# ── Manage: default, remove ──────────────────────────────────────────────

def set_default(user, method_id) -> PaymentMethod:
    with transaction.atomic():
        method = get_active(user, method_id)
        StripeCustomer.objects.select_for_update().filter(pk=method.customer_id).first()
        active_methods(user).filter(is_default=True).exclude(pk=method.pk).update(is_default=False)
        if not method.is_default:
            method.is_default = True
            method.save(update_fields=["is_default", "updated_at"])
    return method


def remove(user, method_id) -> PaymentMethod:
    method = get_active(user, method_id)
    if method.consents.filter(
        kind=PaymentMethodConsent.Kind.AUTOMATIC_ARRANGEMENT, revoked_at__isnull=True,
    ).exists():
        # Decision note §4.1: choose a replacement first. No such arrangement exists yet.
        raise PaymentMethodError(
            "IN_USE", "This card pays for an automatic arrangement. Choose a replacement card first.", 409,
        )
    _stripe_ready()
    try:
        stripe.PaymentMethod.detach(method.stripe_payment_method_id)
    except stripe.error.InvalidRequestError:
        # Already detached in Stripe (e.g. by support): just catch up.
        logger.warning("PaymentMethod %s was already detached", method.pk)
    except Exception:
        logger.exception("Could not detach PaymentMethod %s", method.pk)
        raise PaymentMethodError("REMOVE_FAILED", "We couldn't remove your card. Please try again.", 502)
    return mark_removed(method.stripe_payment_method_id, reason="customer")


def mark_removed(stripe_payment_method_id, *, reason):
    """Mark a card removed; if it was the default, the newest remaining card
    becomes the default. Idempotent (also used by the webhook)."""
    with transaction.atomic():
        method = (
            PaymentMethod.objects.select_for_update()
            .filter(stripe_payment_method_id=stripe_payment_method_id)
            .first()
        )
        if not method or method.status == PaymentMethod.Status.REMOVED:
            return method
        was_default = method.is_default
        method.status = PaymentMethod.Status.REMOVED
        method.is_default = False
        method.removed_at = timezone.now()
        method.removed_reason = reason
        method.save(update_fields=["status", "is_default", "removed_at", "removed_reason", "updated_at"])
        if was_default:
            replacement = (
                PaymentMethod.objects.filter(
                    user_id=method.user_id, customer=method.customer_id, status=PaymentMethod.Status.ACTIVE,
                )
                .order_by("-created_at")
                .first()
            )
            if replacement:
                replacement.is_default = True
                replacement.save(update_fields=["is_default", "updated_at"])
    return method


def apply_card_update(payment_method):
    """Bank reissued the card (new expiry / last 4): keep our display in step."""
    pm = to_plain(payment_method)
    card = pm.get("card") or {}
    PaymentMethod.objects.filter(stripe_payment_method_id=pm.get("id")).update(
        brand=card.get("brand") or "",
        last4=card.get("last4") or "",
        exp_month=card.get("exp_month"),
        exp_year=card.get("exp_year"),
        updated_at=timezone.now(),
    )


# ── Step-up: confirm identity before remove / set default ────────────────

def confirm_identity(user, password):
    """Password re-entry. Social-login accounts have no password, so they
    must have logged in recently instead."""
    if user.has_usable_password():
        if not password or not user.check_password(password):
            raise PaymentMethodError("PASSWORD_INCORRECT", "That password isn't right. Please try again.", 403)
        return
    window = timedelta(minutes=int(getattr(settings, "SAVED_PAYMENT_METHODS_REAUTH_MINUTES", 15)))
    if not user.last_login or timezone.now() - user.last_login > window:
        raise PaymentMethodError("REAUTH_REQUIRED", "Please log in again to change your saved cards.", 403)


# ── Internals ────────────────────────────────────────────────────────────

def _verify_intent(setup: PaymentMethodSetup, intent: dict):
    problems = []
    if intent.get("customer") != setup.customer.stripe_customer_id:
        problems.append("customer")
    if intent.get("livemode") is not setup.customer.livemode:
        problems.append("livemode")
    if problems:
        logger.error("Intent %s does not match setup %s: %s", intent.get("id"), setup.pk, ", ".join(problems))
        raise PaymentMethodError("SETUP_MISMATCH", "We couldn't match this card to your account.", 409)


def _payment_method_details(ref) -> dict:
    if isinstance(ref, dict):
        return ref
    plain = to_plain(ref)
    if isinstance(plain, dict):
        return plain
    _stripe_ready()
    return to_plain(stripe.PaymentMethod.retrieve(ref))


def _detach_quietly(stripe_payment_method_id):
    try:
        stripe.PaymentMethod.detach(stripe_payment_method_id)
    except Exception:
        logger.warning("Could not detach unsaved PaymentMethod %s", stripe_payment_method_id, exc_info=True)


def _finish(setup, status, method=None):
    setup.status = status
    setup.payment_method = method
    setup.save(update_fields=["status", "payment_method", "updated_at"])
    if method and status == PaymentMethodSetup.Status.SAVED:
        setup.consent.payment_method = method
        setup.consent.save(update_fields=["payment_method"])


def _store(setup: PaymentMethodSetup, pm: dict):
    """Turn a Stripe PaymentMethod into a saved card. Caller holds the lock
    on `setup` inside a transaction."""
    Status = PaymentMethodSetup.Status
    pm_id = pm.get("id")

    existing = PaymentMethod.objects.filter(stripe_payment_method_id=pm_id).first()
    if existing:
        _finish(setup, Status.SAVED, existing)
        return
    if pm.get("customer") != setup.customer.stripe_customer_id:
        logger.error("PaymentMethod %s is not attached to setup %s's customer", pm_id, setup.pk)
        raise PaymentMethodError("SETUP_MISMATCH", "We couldn't match this card to your account.", 409)
    if pm.get("type") != PaymentMethod.Type.CARD:
        _detach_quietly(pm_id)
        _finish(setup, Status.FAILED)
        return

    # Serialise saves for this customer: the limit and duplicate checks below
    # must see every card saved before this one.
    StripeCustomer.objects.select_for_update().filter(pk=setup.customer_id).first()
    card = pm.get("card") or {}
    current = PaymentMethod.objects.filter(customer=setup.customer, status=PaymentMethod.Status.ACTIVE)

    fingerprint = card.get("fingerprint") or ""
    duplicate = current.filter(fingerprint=fingerprint).first() if fingerprint else None
    if duplicate:
        _detach_quietly(pm_id)
        _finish(setup, Status.DUPLICATE, duplicate)
        return
    if current.count() >= max_methods():
        _detach_quietly(pm_id)
        _finish(setup, Status.LIMIT_REACHED)
        return

    method = PaymentMethod.objects.create(
        user=setup.user,
        customer=setup.customer,
        method_type=PaymentMethod.Type.CARD,
        stripe_payment_method_id=pm_id,
        brand=card.get("brand") or "",
        last4=card.get("last4") or "",
        exp_month=card.get("exp_month"),
        exp_year=card.get("exp_year"),
        fingerprint=fingerprint,
        is_default=not current.filter(is_default=True).exists(),
        allowed_usage=PaymentMethod.Usage.ON_SESSION,
    )
    _finish(setup, Status.SAVED, method)
