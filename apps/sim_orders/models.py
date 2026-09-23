import secrets
import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone

# The activation code is 'ZM' + 6 digits (matches the old WordPress format),
# and is valid for 24 hours after the SIM is purchased.
ACTIVATION_CODE_TTL_HOURS = 24


def generate_activation_code() -> str:
    """Cryptographically secure 'ZM######' code (secrets, never random)."""
    return "ZM" + f"{secrets.randbelow(10 ** 6):06d}"


class SimProduct(models.Model):
    """A SIM that can be bought (name, price, the plan it comes with)."""

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    plan_name = models.CharField(max_length=120, blank=True, default="")
    description = models.TextField(blank=True, default="")
    price_pence = models.PositiveIntegerField(help_text="Price in pence, e.g. 999 = £9.99")
    currency = models.CharField(max_length=3, default="gbp")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    @property
    def price_display(self):
        return f"£{self.price_pence / 100:.2f}"

    def __str__(self):
        return f"{self.name} ({self.price_display})"


class SimOrder(models.Model):
    """One SIM purchase. Paid via Stripe; status driven by the Stripe webhook."""

    STATUS_PENDING = "pending_payment"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending payment"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    order_ref = models.CharField(max_length=24, unique=True, editable=False)
    product = models.ForeignKey(SimProduct, on_delete=models.PROTECT, related_name="orders")

    customer_name = models.CharField(max_length=120, blank=True, default="")
    email = models.EmailField(db_index=True)

    amount_pence = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="gbp")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)

    stripe_session_id = models.CharField(max_length=255, blank=True, default="")
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.order_ref:
            self.order_ref = "SIM-" + uuid.uuid4().hex[:10].upper()
        super().save(*args, **kwargs)

    @property
    def amount_display(self):
        return f"£{self.amount_pence / 100:.2f}"

    def __str__(self):
        return f"{self.order_ref} · {self.amount_display} · {self.get_status_display()}"


class SimActivationCode(models.Model):
    """The 'ZM######' code issued when a SIM is bought. Emailed to the customer,
    valid 24h, single-use. The activation page validates the entered code + email
    against this record. (SIM serial is captured later, at activation time.)
    """

    code = models.CharField(max_length=12, db_index=True)   # e.g. ZM123456
    email = models.EmailField(db_index=True)
    order = models.ForeignKey(SimOrder, on_delete=models.CASCADE, related_name="activation_codes")

    sim_serial = models.CharField(max_length=25, blank=True, default="")  # filled at activation
    attempts = models.PositiveSmallIntegerField(default=0)                # brute-force guard
    is_used = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["email", "code", "is_used"], name="sim_actcode_lookup_idx"),
        ]

    @classmethod
    def issue_for_order(cls, order: "SimOrder") -> "SimActivationCode":
        return cls.objects.create(
            code=generate_activation_code(),
            email=order.email,
            order=order,
            expires_at=timezone.now() + timedelta(hours=ACTIVATION_CODE_TTL_HOURS),
        )

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    def __str__(self):
        state = "used" if self.is_used else ("expired" if self.is_expired else "active")
        return f"{self.code} ({state})"


class SimCartOrder(models.Model):
    """A SIM order placed from the CHECKOUT page (separate from device orders,
    which go to the `orders` app's BqOrder). Stores the full checkout payload as
    JSON plus a few queryable fields, mirroring how BqOrder works so the two
    order streams stay symmetrical.
    """

    STATUS_RECEIVED = "received"
    STATUS_PENDING = "pending_payment"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_RECEIVED, "Received"),
        (STATUS_PENDING, "Pending payment"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    order_ref = models.CharField(max_length=24, unique=True, editable=False)
    email = models.EmailField(db_index=True)
    customer_name = models.CharField(max_length=160, blank=True, default="")
    order_type = models.CharField(max_length=20, default="sim")  # "sim" | "mixed"
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_RECEIVED)
    raw_data = models.JSONField()  # full checkout payload (billing, cart, totals, simType)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.order_ref:
            self.order_ref = "SIMO-" + uuid.uuid4().hex[:10].upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.order_ref} · {self.email} · {self.get_status_display()}"
