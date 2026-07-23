import uuid

from django.db import models


class RechargeModule(models.Model):
    """The three toggleable features shown on the WP 'Active Modules' table:
    Recharge, Top Up, Pending Bill — each with an Enabled/Disabled state.
    """

    KEY_CHOICES = [
        ("recharge", "Recharge"),
        ("topup", "Top Up"),
        ("pending_bill", "Pending Bill"),
    ]

    key = models.CharField(max_length=32, choices=KEY_CHOICES, unique=True)
    name = models.CharField(max_length=64)          # display name, e.g. "Top Up"
    category = models.CharField(max_length=64)       # mirrors WP "Category" column
    shortcode = models.CharField(max_length=64)      # mirrors WP "Shortcode" column, e.g. "[topup]"
    enabled = models.BooleanField(default=True)      # mirrors WP "Status" column
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"{self.name} ({'Enabled' if self.enabled else 'Disabled'})"


class RechargeOrder(models.Model):
    """One recharge = one payable order, exactly like the WooCommerce recharge
    orders (amount + status + module). Stripe is the payment engine.
    """

    STATUS_PENDING = "pending_payment"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_REFUNDED = "refunded"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending payment"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_REFUNDED, "Refunded"),
    ]

    order_ref = models.CharField(max_length=24, unique=True, editable=False)
    module = models.CharField(max_length=32, choices=RechargeModule.KEY_CHOICES, default="recharge")

    msisdn = models.CharField(max_length=20)                 # phone number being recharged
    customer_name = models.CharField(max_length=120, blank=True, default="")
    customer_email = models.EmailField(blank=True, default="")

    # Money is ALWAYS stored as an integer number of pence. Never floats.
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
            self.order_ref = "RC-" + uuid.uuid4().hex[:10].upper()
        super().save(*args, **kwargs)

    @property
    def amount_display(self):
        return f"£{self.amount_pence / 100:.2f}"

    def __str__(self):
        return f"{self.order_ref} · {self.amount_display} · {self.get_status_display()}"
