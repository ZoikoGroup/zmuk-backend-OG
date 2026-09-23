"""
Malima / Orange IoT — data models.

Three tables:

  SimInventory     — pool of SIM identifiers (msisdn/imsi/iccid). Loaded by ops
                     ahead of time. `status` controls availability.
  SimReservation   — links an inventory row to an order at allocation time.
                     Atomic so the same SIM is never allocated twice.
  MalimaOrder      — the capture record: full Orange request/response per
                     line, customer context, totals, payment intent id.

The split lets you:
  • Reload inventory without touching past orders.
  • Audit reservations independently of orders.
  • Reconcile from the Orange response stored on MalimaOrderLine.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone


# ── Inventory ────────────────────────────────────────────────────────────────

class SimInventory(models.Model):
    """A single SIM record from the pool."""

    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        RESERVED = "reserved", "Reserved"
        ACTIVATED = "activated", "Activated"
        SUSPENDED = "suspended", "Suspended"
        RETIRED = "retired", "Retired"
        # Mirror Orange's ACTIVATED_FOR_TEST so ops can flag test SIMs.
        TEST = "test", "Test (ACTIVATED_FOR_TEST)"

    msisdn = models.CharField(max_length=20, unique=True, db_index=True)
    imsi = models.CharField(max_length=20, unique=True, db_index=True)
    iccid = models.CharField(max_length=22, unique=True, db_index=True)

    sim_type = models.CharField(
        max_length=10,
        choices=[("eSIM", "eSIM"), ("pSIM", "pSIM")],
        default="eSIM",
        db_index=True,
    )
    roaming_zone = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        help_text="Optional — restrict this SIM to a specific zone (e.g. 'Europe').",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices,
        default=Status.AVAILABLE, db_index=True,
    )

    # Free-form notes / batch tag (e.g. "Q2-2026-FR-batch-3"). Useful for
    # reconciling shipments and recalling a batch.
    batch = models.CharField(max_length=64, blank=True, default="", db_index=True)
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "SIM inventory"
        verbose_name_plural = "SIM inventory"
        ordering = ("status", "id")
        indexes = [
            # The hot path: find available SIMs of a given type/zone fast.
            models.Index(fields=["status", "sim_type", "roaming_zone"]),
        ]

    def __str__(self) -> str:
        return f"{self.sim_type} {self.msisdn} ({self.status})"


# ── Reservations ─────────────────────────────────────────────────────────────

class SimReservation(models.Model):
    """A SIM held against a specific cart unit for the duration of an order."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        CONSUMED = "consumed", "Consumed"      # order succeeded
        RELEASED = "released", "Released"      # order failed / cancelled
        EXPIRED = "expired", "Expired"         # never confirmed in time

    reservation_id = models.CharField(max_length=64, unique=True, db_index=True)

    sim = models.ForeignKey(
        SimInventory, on_delete=models.PROTECT, related_name="reservations",
    )
    # Free-form caller payload — useful if you ever rebuild the cart from logs.
    cart_index = models.IntegerField()
    unit_index = models.IntegerField()
    plan_id = models.CharField(max_length=64, blank=True, default="")

    status = models.CharField(
        max_length=16, choices=Status.choices,
        default=Status.ACTIVE, db_index=True,
    )

    # When the reservation should be auto-released if the order never completes.
    # Defaults are set in the view (15 min); admin can override.
    expires_at = models.DateTimeField()

    created_at = models.DateTimeField(auto_now_add=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["status", "expires_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.reservation_id} → {self.sim.msisdn}"


# ── Capture record ───────────────────────────────────────────────────────────

class MalimaOrder(models.Model):
    """One order = one checkout. May have multiple lines (one per SIM)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial — some lines failed"
        FAILED = "failed", "Failed"

    source = models.CharField(
        max_length=64, default="zoiko-orbit-checkout",
        help_text="Where the request came from. Set by the proxy.",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices,
        default=Status.PENDING, db_index=True,
    )

    # Customer context — denormalised so the order record is self-contained
    # even if the customer changes their saved address later.
    email = models.EmailField(blank=True, default="", db_index=True)
    customer_name = models.CharField(max_length=255, blank=True, default="")
    billing_address = models.JSONField(default=dict, blank=True)
    shipping_address = models.JSONField(default=dict, blank=True)

    # Cross-references to other systems.
    stripe_payment_intent_id = models.CharField(
        max_length=128, blank=True, default="", db_index=True,
    )
    bequick_order_id = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        help_text="Kept for legacy / migration cross-references. May be empty.",
    )

    # Totals — store as decimals so reporting doesn't have to parse the JSON blob.
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    activation_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    currency = models.CharField(max_length=8, default="USD")

    # Everything else from the proxy (the original Next.js context object).
    # Kept as JSON for forward-compat — if Next.js adds a new field, we
    # don't need a migration to store it.
    raw_payload = models.JSONField(default=dict, blank=True)

    captured_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-captured_at",)
        indexes = [
            models.Index(fields=["status", "captured_at"]),
        ]

    def __str__(self) -> str:
        return f"Order #{self.pk} ({self.status}, {self.total} {self.currency})"


class MalimaOrderLine(models.Model):
    """One Orange productOrder per cart unit. Stores the raw request/response."""

    order = models.ForeignKey(
        MalimaOrder, on_delete=models.CASCADE, related_name="lines",
    )
    cart_index = models.IntegerField()
    unit_index = models.IntegerField(default=0)

    plan_id = models.CharField(max_length=64, blank=True, default="")
    plan_name = models.CharField(max_length=255, blank=True, default="")
    sim_type = models.CharField(max_length=10, blank=True, default="")

    # Reservation that fed this line (so you can trace back to inventory).
    reservation = models.ForeignKey(
        SimReservation, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="lines",
    )
    msisdn = models.CharField(max_length=20, blank=True, default="", db_index=True)
    imsi = models.CharField(max_length=20, blank=True, default="")
    iccid = models.CharField(max_length=22, blank=True, default="", db_index=True)

    # Orange outcome.
    orange_order_id = models.CharField(
        max_length=128, blank=True, default="", db_index=True,
    )
    http_status = models.IntegerField(default=0)
    ok = models.BooleanField(default=False)
    error = models.CharField(max_length=512, blank=True, default="")

    # Full request/response captured verbatim for reconciliation/debugging.
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("order", "cart_index", "unit_index")
        indexes = [
            models.Index(fields=["ok", "created_at"]),
        ]

    def __str__(self) -> str:
        flag = "✓" if self.ok else "✗"
        return f"{flag} line {self.cart_index}.{self.unit_index} → {self.msisdn or '—'}"
