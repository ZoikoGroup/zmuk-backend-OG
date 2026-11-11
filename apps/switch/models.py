"""Switch & Save: a customer's request to move to Zoiko Mobile.

Payment is not taken at submit (decided 2026-11-11): the customer sends a
request first, and the order and line are linked later (`order`, `line`).

PAC/STAC codes follow the add_line rules (decision C13): stored only
Fernet-encrypted in `encrypted_code`, never returned by the API, and blanked
by `purge_switch_codes` once no longer needed.
"""

import secrets

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from apps.add_line.models import SimType, Term

_REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
REFERENCE_PREFIX = "ZM-SW-"


def generate_reference() -> str:
    """Random, non-sequential reference, so it can't be guessed for lookups."""
    return REFERENCE_PREFIX + "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(8))


class SwitchRequest(models.Model):
    """A customer request to switch (port) their number to Zoiko Mobile."""

    class Status(models.TextChoices):
        PENDING = "pending", "Request received"
        UNDER_REVIEW = "under_review", "Under review"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    # No further work expected; codes can be purged after PAC_RETENTION_DAYS.
    CLOSED_STATUSES = (Status.COMPLETED, Status.FAILED, Status.CANCELLED)

    class Route(models.TextChoices):
        KEEP_PAC = "keep_pac", "Keep my number (PAC)"
        NEW_STAC = "new_stac", "New number, end my old service (STAC)"
        NEW_KEEP = "new_keep", "New number, keep my old service"

    class CodeType(models.TextChoices):
        PAC = "PAC", "PAC"
        STAC = "STAC", "STAC"

    # Which code each route needs (None = no code).
    ROUTE_CODE = {
        Route.KEEP_PAC: CodeType.PAC,
        Route.NEW_STAC: CodeType.STAC,
        Route.NEW_KEEP: None,
    }

    PROVIDER_CHOICES = [
        ("EE", "EE"),
        ("O2", "O2"),
        ("Vodafone", "Vodafone"),
        ("Three", "Three"),
        ("Sky Mobile", "Sky Mobile"),
        ("giffgaff", "giffgaff"),
        ("Tesco Mobile", "Tesco Mobile"),
        ("iD Mobile", "iD Mobile"),
        ("VOXI", "VOXI"),
        ("SMARTY", "SMARTY"),
        ("Lebara", "Lebara"),
        ("Lycamobile", "Lycamobile"),
        ("BT Mobile", "BT Mobile"),
        ("Virgin Mobile", "Virgin Mobile"),
        ("Other UK Provider", "Other UK Provider"),
    ]

    reference = models.CharField(max_length=20, unique=True, editable=False)
    # Set when the form was submitted with a valid token. Never matched by email.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="switch_requests",
    )

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
    # GB per month.
    current_data_allowance = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )

    # ── Step 3: Plan ──────────────────────────────────────────────────────
    plan = models.ForeignKey(
        "plans.Plan", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="switch_requests",
    )
    # Plan name at the time of the request (kept if the plan is later renamed).
    selected_plan = models.CharField(max_length=150, blank=True)
    contract_term = models.CharField(max_length=3, choices=Term.choices, blank=True)
    sim_type = models.CharField(max_length=4, choices=SimType.choices, blank=True)
    promo_code = models.CharField(max_length=50, blank=True)

    # ── Step 4: Number transfer ───────────────────────────────────────────
    switch_route = models.CharField(max_length=10, choices=Route.choices, blank=True)
    # The number being moved, as 447XXXXXXXXX (Transatel format).
    port_number = models.CharField(max_length=24, blank=True)
    pac_later = models.BooleanField(default=False)
    code_type = models.CharField(max_length=4, choices=CodeType.choices, blank=True)
    encrypted_code = models.TextField(blank=True)
    code_received_at = models.DateTimeField(null=True, blank=True)
    code_purged_at = models.DateTimeField(null=True, blank=True)

    # ── Authorisation (all three must be true to submit) ──────────────────
    transfer_authorised = models.BooleanField(default=False)
    timeline_acknowledged = models.BooleanField(default=False)
    terms_accepted = models.BooleanField(default=False)
    opt_in_news = models.BooleanField(default=False)
    opt_in_texts = models.BooleanField(default=False)

    # ── Processing ────────────────────────────────────────────────────────
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    # Filled in once the customer pays and the line is created (not at submit).
    order = models.OneToOneField(
        "add_line.AddLineOrder", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="switch_request",
    )
    line = models.OneToOneField(
        "add_line.Line", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="switch_request",
    )
    # Set by ops to override the calculated estimate.
    estimated_completion_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Switch Request"
        verbose_name_plural = "Switch Requests"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._loaded_status = None
        # Optionally set by callers so history records who changed the status.
        self._status_actor = None

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_status = instance.__dict__.get("status")
        return instance

    def __str__(self):
        return f"{self.reference} · {self.first_name} {self.last_name} → {self.selected_plan} ({self.status})"

    @property
    def required_code_type(self):
        return self.ROUTE_CODE.get(self.switch_route)

    def save(self, *args, **kwargs):
        creating = self._state.adding
        if not self.reference:
            self.reference = generate_reference()
            while SwitchRequest.objects.filter(reference=self.reference).exists():
                self.reference = generate_reference()
        status_changed = (
            not creating and self._loaded_status is not None and self.status != self._loaded_status
        )
        if self.status == self.Status.COMPLETED and not self.completed_at:
            self.completed_at = timezone.now()
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = list(kwargs["update_fields"]) + ["completed_at"]

        super().save(*args, **kwargs)

        if creating or status_changed:
            SwitchStatusEvent.objects.create(
                switch_request=self,
                kind=SwitchStatusEvent.Kind.CREATED if creating else SwitchStatusEvent.Kind.STATUS_CHANGED,
                status_before="" if creating else self._loaded_status,
                status_after=self.status,
                actor=self._status_actor,
            )
        self._loaded_status = self.status


class SwitchStatusEvent(models.Model):
    """Append-only history of a switch request, shown to the customer."""

    class Kind(models.TextChoices):
        CREATED = "created", "Request received"
        STATUS_CHANGED = "status_changed", "Status changed"
        CODE_RECEIVED = "code_received", "Switching code received"

    switch_request = models.ForeignKey(
        SwitchRequest, on_delete=models.CASCADE, related_name="events"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status_before = models.CharField(max_length=20, blank=True)
    status_after = models.CharField(max_length=20, blank=True)
    # Null when the change came from the customer or the system.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    # Internal only; never returned to the customer.
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.switch_request.reference} · {self.kind} {self.status_before}→{self.status_after}"


class SwitchFeedback(models.Model):
    """Customer rating of the switch experience (one per request)."""

    switch_request = models.OneToOneField(
        SwitchRequest, on_delete=models.CASCADE, related_name="feedback"
    )
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comments = models.TextField(blank=True, max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.switch_request.reference} · {self.rating}/5"
