from django.db import models


class SwitchRequest(models.Model):
    """A customer request to switch (port) their number to Zoiko Mobile."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    PROVIDER_CHOICES = [
        ("EE", "EE"),
        ("O2", "O2"),
        ("Vodafone", "Vodafone"),
        ("Three", "Three"),
        ("BT Mobile", "BT Mobile"),
        ("Sky Mobile", "Sky Mobile"),
        ("Tesco Mobile", "Tesco Mobile"),
        ("giffgaff", "giffgaff"),
        ("Other", "Other"),
    ]

    PLAN_CHOICES = [
        ("Zoiko Unlimited Data", "Zoiko Unlimited Data"),
        ("Zoiko Elite 100GB", "Zoiko Elite 100GB"),
        ("Zoiko Max 30GB", "Zoiko Max 30GB"),
        ("Zoiko Standard 10GB", "Zoiko Standard 10GB"),
        ("Zoiko Plus 3GB", "Zoiko Plus 3GB"),
        ("Zoiko Connect 1GB", "Zoiko Connect 1GB"),
    ]

    # ── Step 1: Your Details ──────────────────────────────────────────────
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField()
    mobile = models.CharField(max_length=20)
    # Labelled "Billing Address / Postcode" on the form, so allow a full line.
    postcode = models.CharField(max_length=255)

    # ── Step 2: Current Service (all optional) ────────────────────────────
    current_provider = models.CharField(
        max_length=50, choices=PROVIDER_CHOICES, blank=True
    )
    current_plan_cost = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )
    current_data_allowance = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )

    # ── Step 3: Select Plan ───────────────────────────────────────────────
    selected_plan = models.CharField(max_length=100, choices=PLAN_CHOICES)

    # ── Authorisation (all three must be true to submit) ──────────────────
    transfer_authorised = models.BooleanField(default=False)
    timeline_acknowledged = models.BooleanField(default=False)
    terms_accepted = models.BooleanField(default=False)

    # ── Switch processing ─────────────────────────────────────────────────
    # PAC = keep your number; STAC = leave without keeping it (Ofcom).
    pac_code = models.CharField(max_length=20, blank=True)
    stac_code = models.CharField(max_length=20, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Switch Request"
        verbose_name_plural = "Switch Requests"

    def __str__(self):
        return f"{self.first_name} {self.last_name} → {self.selected_plan} ({self.status})"
