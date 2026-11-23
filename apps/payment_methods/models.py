"""Saved payment methods (decision note, 23 Nov 2026).

Stripe holds the card. We keep only references and display details: never
the card number, CVC or any client secret. Named "payment methods" rather
than "cards" so Bacs Direct Debit can be added later without a new model.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class StripeCustomer(models.Model):
    """One Stripe Customer per user per Stripe mode (test / live)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="stripe_customers")
    stripe_customer_id = models.CharField(max_length=255, unique=True)
    livemode = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "livemode"], name="one_stripe_customer_per_mode"),
        ]

    def __str__(self):
        return f"{self.user} · {self.stripe_customer_id}"


class PaymentMethod(models.Model):
    class Type(models.TextChoices):
        CARD = "card", "Card"
        # BACS_DEBIT = "bacs_debit" — future phase, not part of this release.

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        REMOVED = "removed", "Removed"

    class Usage(models.TextChoices):
        # Payments the customer starts themselves (the only kind used now).
        ON_SESSION = "on_session", "Customer present"
        # Only for a separately approved and consented automatic arrangement.
        OFF_SESSION = "off_session", "Automatic arrangement"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payment_methods")
    customer = models.ForeignKey(StripeCustomer, on_delete=models.PROTECT, related_name="payment_methods")
    method_type = models.CharField(max_length=20, choices=Type.choices, default=Type.CARD)
    stripe_payment_method_id = models.CharField(max_length=255, unique=True)
    brand = models.CharField(max_length=30, blank=True, default="")
    last4 = models.CharField(max_length=4, blank=True, default="")
    exp_month = models.PositiveSmallIntegerField(null=True, blank=True)
    exp_year = models.PositiveSmallIntegerField(null=True, blank=True)
    # Duplicate detection only. Never sent to the frontend.
    fingerprint = models.CharField(max_length=64, blank=True, default="")
    is_default = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    allowed_usage = models.CharField(max_length=20, choices=Usage.choices, default=Usage.ON_SESSION)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_reason = models.CharField(max_length=40, blank=True, default="")

    class Meta:
        ordering = ["-is_default", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user"], condition=Q(is_default=True, status="active"), name="one_default_payment_method",
            ),
        ]

    def __str__(self):
        return f"{self.brand} ••{self.last4} ({self.user})"


class PaymentMethodConsent(models.Model):
    """What the customer agreed to, in the exact wording shown to them."""

    class Kind(models.TextChoices):
        # (A) Save for payments the customer chooses to make.
        SAVE_FOR_MY_PAYMENTS = "save_for_my_payments", "Save for my own payments"
        # (B) A specific automatic arrangement. None approved yet.
        AUTOMATIC_ARRANGEMENT = "automatic_arrangement", "Automatic payment arrangement"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payment_method_consents")
    payment_method = models.ForeignKey(
        PaymentMethod, on_delete=models.SET_NULL, null=True, blank=True, related_name="consents",
    )
    kind = models.CharField(max_length=30, choices=Kind.choices)
    wording_version = models.CharField(max_length=40)
    wording_text = models.TextField()
    # Kind B only: which approved arrangement (plan, service) it covers.
    arrangement_reference = models.CharField(max_length=100, blank=True, default="")
    accepted_at = models.DateTimeField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default="")
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-accepted_at"]


class PaymentMethodSetup(models.Model):
    """One attempt to save a card: a SetupIntent from the dashboard, or a
    PaymentIntent with "save this card" ticked at checkout.

    The browser's confirm call, the webhook and the checkout's payment
    success can all finish the same attempt; this row makes that idempotent.
    """

    class Source(models.TextChoices):
        DASHBOARD = "dashboard", "Dashboard (add a card)"
        CHECKOUT = "checkout", "Main checkout"
        ADD_LINE = "add_line", "Add-line checkout"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SAVED = "saved", "Saved"
        DUPLICATE = "duplicate", "Already saved"
        LIMIT_REACHED = "limit_reached", "Limit reached"
        FAILED = "failed", "Failed"

    stripe_intent_id = models.CharField(max_length=255, unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payment_method_setups")
    customer = models.ForeignKey(StripeCustomer, on_delete=models.PROTECT, related_name="setups")
    consent = models.OneToOneField(PaymentMethodConsent, on_delete=models.PROTECT, related_name="setup")
    source = models.CharField(max_length=20, choices=Source.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    payment_method = models.ForeignKey(
        PaymentMethod, on_delete=models.SET_NULL, null=True, blank=True, related_name="setups",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def finished(self) -> bool:
        return self.status != self.Status.PENDING
