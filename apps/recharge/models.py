import uuid
import hashlib

from django.db import models


class RechargeModule(models.Model):
    """The three toggleable features: Recharge, Top Up, Pending Bill."""

    KEY_CHOICES = [
        ("recharge", "Recharge"),
        ("topup", "Top Up"),
        ("pending_bill", "Pending Bill"),
    ]

    key = models.CharField(max_length=32, choices=KEY_CHOICES, unique=True)
    name = models.CharField(max_length=64)
    category = models.CharField(max_length=64)
    shortcode = models.CharField(max_length=64)
    enabled = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"{self.name} ({'Enabled' if self.enabled else 'Disabled'})"


class RechargeProduct(models.Model):
    """A recharge plan (mirrors WooCommerce products in the recharge category).

    Example: "Zoiko Elite 100GB" at £28.34 → price_pence=2834, rate_plan="MVNA Wholesale PAYM 7"
    """

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    module = models.CharField(
        max_length=32,
        choices=RechargeModule.KEY_CHOICES,
        default="recharge",
        db_index=True,
    )
    price_pence = models.PositiveIntegerField(help_text="Price in pence (e.g. 2834 = £28.34)")
    currency = models.CharField(max_length=3, default="GBP")
    short_description = models.CharField(max_length=512, blank=True, default="")
    description = models.TextField(blank=True, default="")
    # Transatel rate plan sent with reactivation. Falls back to settings default if blank.
    rate_plan = models.CharField(max_length=128, blank=True, default="")
    # Extra attributes (validity_days, data_gb, etc.) stored as JSON.
    attributes = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "price_pence"]

    @property
    def price_display(self):
        return f"£{self.price_pence / 100:.2f}"

    def __str__(self):
        return f"{self.name} — {self.price_display}"


class RechargeOrder(models.Model):
    """One recharge = one payable order. Stripe is the payment engine."""

    STATUS_PENDING = "pending_payment"
    STATUS_PROCESSING = "processing"   # paid, reactivation running
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_REFUNDED = "refunded"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending payment"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_REFUNDED, "Refunded"),
    ]

    order_ref = models.CharField(max_length=24, unique=True, editable=False)
    module = models.CharField(max_length=32, choices=RechargeModule.KEY_CHOICES, default="recharge")

    msisdn = models.CharField(max_length=20, db_index=True)
    sim_serial = models.CharField(max_length=64, blank=True, default="", db_index=True)
    sim_iccid = models.CharField(max_length=32, blank=True, default="")
    customer_name = models.CharField(max_length=120, blank=True, default="")
    customer_email = models.EmailField(blank=True, default="")

    product = models.ForeignKey(
        RechargeProduct, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="orders",
    )
    amount_pence = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="gbp")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)

    stripe_session_id = models.CharField(max_length=255, blank=True, default="")
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.order_ref:
            self.order_ref = "RC-" + uuid.uuid4().hex[:10].upper()
        super().save(*args, **kwargs)

    @property
    def amount_display(self):
        return f"£{self.amount_pence / 100:.2f}"

    def __str__(self):
        return f"{self.order_ref} · {self.amount_display} · {self.get_status_display()}"


class ReactivationAttempt(models.Model):
    """Idempotency guard + audit trail for Transatel SIM reactivation.

    Unique constraint on (order, sim_serial) means the same order can never
    trigger two reactivation calls — even if the webhook and the synchronous
    response both try.
    """

    STATUS_PENDING = "pending"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
        (STATUS_SKIPPED, "Skipped"),
    ]

    order = models.ForeignKey(RechargeOrder, on_delete=models.CASCADE, related_name="reactivation_attempts")
    sim_serial = models.CharField(max_length=64, db_index=True)
    rate_plan = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    provider_transaction_id = models.CharField(max_length=128, blank=True, default="")
    error = models.TextField(blank=True, default="")
    response_data = models.JSONField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=64, unique=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "sim_serial"], name="uq_reactivation_order_sim"),
        ]

    @staticmethod
    def make_idempotency_key(order_id, sim_serial):
        raw = f"reactivate:{order_id}:{sim_serial}".encode()
        return hashlib.sha256(raw).hexdigest()[:48]

    def __str__(self):
        return f"Reactivation {self.order.order_ref} / ***{self.sim_serial[-4:]} — {self.status}"


class TransatelLog(models.Model):
    """Audit log for every Transatel API call made during recharge."""

    ACTION_CHOICES = [
        ("validate_phone", "Validate Phone"),
        ("reactivate", "Reactivate SIM"),
        ("get_subscriber", "Get Subscriber"),
    ]

    order = models.ForeignKey(
        RechargeOrder, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="transatel_logs",
    )
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, db_index=True)
    sim_serial_masked = models.CharField(max_length=20, blank=True, default="")
    msisdn_masked = models.CharField(max_length=20, blank=True, default="")

    request_url = models.CharField(max_length=512, blank=True, default="")
    request_method = models.CharField(max_length=8, blank=True, default="")
    request_body = models.JSONField(null=True, blank=True)

    response_status = models.IntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")

    success = models.BooleanField(default=False)
    duration_ms = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        tag = "OK" if self.success else "FAIL"
        return f"[{tag}] {self.action} — {self.created_at:%Y-%m-%d %H:%M}"
