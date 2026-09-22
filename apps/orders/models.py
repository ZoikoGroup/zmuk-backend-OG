import uuid

from django.db import models


class BqOrder(models.Model):
    raw_data = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Bq Order #{self.id}"


class CheckoutPayment(models.Model):
    """Tracks a checkout-page PaymentIntent. Created BEFORE payment (holds
    the full cart/billing payload), confirmed by the webhook AFTER Stripe
    confirms payment. This is the source of truth — the browser's word alone
    is never trusted for order creation.
    """

    STATUS_PENDING = "pending"
    STATUS_PAID = "paid"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PAID, "Paid"),
        (STATUS_FAILED, "Failed"),
    ]

    order_ref = models.CharField(max_length=24, unique=True, editable=False)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, default="")
    email = models.EmailField(db_index=True, blank=True, default="")
    amount_pence = models.PositiveIntegerField()
    currency = models.CharField(max_length=8, default="gbp")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    payload = models.JSONField()  # full { billingAddress, shippingAddress, cart, totals, coupon }
    processed = models.BooleanField(default=False)  # true once order rows are created
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.order_ref:
            self.order_ref = "CHK-" + uuid.uuid4().hex[:10].upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.order_ref} · {self.status}"
