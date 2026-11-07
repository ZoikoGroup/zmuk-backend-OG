"""Data model for the Roaming & Travel card (CT-001, migration decisions ZMUK-ROAM-005 v2.0).

The roaming tariffs already exist (Transatel, 3+ years). This app holds a
versioned copy of them so the dashboard can show them accurately:

    * `Destination` is geography only: ISO code, name, kind. It never says
      whether roaming is included anywhere.
    * `RegisterVersion` is one imported register file from Product/Operations.
      Only one version is active; quotes always record which one they used.
    * `RegisterEntry` is one row per plan SKU x plan version x destination x
      effective period, with the §04 field list. Rows with a blank `plan_sku`
      are the published general (non-personalised) rates.
    * `PlanTariffMap` links our `Plan` + term to the register's SKU/version.
    * `RoamingQuote` is an immutable snapshot of every personalised quote.

Geography, rating zone and plan inclusion are separate fields and are never
derived from one another (ROAM-005 §02 B).
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

# Our Line.term values ("30D", "12M", "24M"); blank means any term.
TERM_CHOICES = [("", "Any term"), ("30D", "30 days"), ("12M", "12 months"), ("24M", "24 months")]


class Destination(models.Model):
    """A country, territory or special network a customer can roam on."""

    class Kind(models.TextChoices):
        COUNTRY = "country", "Country"
        TERRITORY = "territory", "Territory"
        MARITIME = "maritime", "Maritime network"
        AIRCRAFT = "aircraft", "Aircraft network"

    # ISO 3166-1 alpha-2 where one exists (FR, JE, IM); otherwise our own code
    # (MARITIME, AIRCRAFT, CY-NORTH).
    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.COUNTRY)
    parent_code = models.CharField(
        max_length=16, blank=True, default="",
        help_text="Sovereign country for a territory, e.g. FR for Guadeloupe.",
    )
    aliases = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Other names customers search for, comma separated.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def is_high_risk(self) -> bool:
        return self.kind in (self.Kind.MARITIME, self.Kind.AIRCRAFT)


class RegisterVersion(models.Model):
    """One imported destination-and-plan register (ROAM-005 §02 A)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        RETIRED = "retired", "Retired"

    label = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)
    source_system = models.CharField(max_length=128, blank=True, default="")
    # SHA-256 of the imported file, so the same export can't be loaded twice.
    checksum = models.CharField(max_length=64, unique=True)
    validation_date = models.DateField(
        help_text="Date the data was last checked against the live tariff/billing records.",
    )
    notes = models.TextField(blank=True, default="")

    row_count = models.PositiveIntegerField(default=0)
    unresolved_count = models.PositiveIntegerField(default=0)

    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    imported_at = models.DateTimeField(auto_now_add=True)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-imported_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["status"], condition=Q(status="active"), name="roaming_one_active_register",
            ),
        ]

    def __str__(self):
        return f"{self.label} ({self.get_status_display()})"


class RegisterEntry(models.Model):
    """One plan version x destination x effective period (ROAM-005 §04)."""

    class Availability(models.TextChoices):
        AVAILABLE = "available", "Available"
        NOT_AVAILABLE = "not_available", "Not available"
        UNKNOWN = "unknown", "Unknown"

    class Inclusion(models.TextChoices):
        INCLUDED = "included", "Included in plan"
        NOT_INCLUDED = "not_included", "Not included (charged)"
        UNKNOWN = "unknown", "Unknown"

    class Exhaustion(models.TextChoices):
        CHARGE = "charge", "Charged at the out-of-bundle rate"
        BLOCK = "block", "Stopped by an existing cap/bar"
        CUSTOMER_CHOICE = "customer_choice", "Customer chooses"
        UNKNOWN = "unknown", "Unknown"

    class RowStatus(models.TextChoices):
        RESOLVED = "resolved", "Resolved"
        UNRESOLVED = "unresolved", "Unresolved"

    version = models.ForeignKey(RegisterVersion, on_delete=models.CASCADE, related_name="entries")

    # Plan identity. Blank plan_sku = published general rates for the destination.
    plan_sku = models.CharField(max_length=64, blank=True, default="", db_index=True)
    plan_version = models.CharField(max_length=32, blank=True, default="")
    contract_duration = models.CharField(max_length=16, blank=True, default="")
    customer_tariff_id = models.CharField(max_length=64, blank=True, default="")

    # Geography and charging zone, kept apart.
    territory_code = models.CharField(max_length=16)
    destination = models.ForeignKey(
        Destination, on_delete=models.PROTECT, null=True, blank=True, related_name="register_entries"
    )
    rating_zone = models.CharField(max_length=32, blank=True, default="")
    supplier_availability = models.CharField(
        max_length=16, choices=Availability.choices, default=Availability.UNKNOWN
    )
    inclusion = models.CharField(max_length=16, choices=Inclusion.choices, default=Inclusion.UNKNOWN)

    # Inclusive allowance. Null = unknown, never zero.
    included_data_mb = models.PositiveIntegerField(null=True, blank=True)
    included_voice_minutes = models.PositiveIntegerField(null=True, blank=True)
    voice_unlimited = models.BooleanField(default=False)
    included_sms = models.PositiveIntegerField(null=True, blank=True)
    sms_unlimited = models.BooleanField(default=False)
    fair_use_terms = models.TextField(blank=True, default="")

    # Out-of-bundle rates in `currency`, on the `vat_basis` given. Null = unknown.
    rate_data_per_mb = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    rate_voice_to_uk_per_min = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    rate_voice_local_per_min = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    rate_voice_incoming_per_min = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    rate_sms = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    rate_mms = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    rate_units_rounding = models.CharField(max_length=255, blank=True, default="")
    vat_basis = models.CharField(max_length=32, blank=True, default="")
    currency = models.CharField(max_length=3, default="GBP")

    exhaustion_action = models.CharField(max_length=16, choices=Exhaustion.choices, default=Exhaustion.UNKNOWN)
    applicable_spend_control = models.CharField(max_length=255, blank=True, default="")

    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)

    source_system = models.CharField(max_length=128, blank=True, default="")
    source_record_id = models.CharField(max_length=128, blank=True, default="")
    validation_date = models.DateField(null=True, blank=True)

    row_number = models.PositiveIntegerField(help_text="Line number in the imported file.")
    row_status = models.CharField(max_length=16, choices=RowStatus.choices, default=RowStatus.RESOLVED, db_index=True)
    unresolved_reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["version", "row_number"]
        verbose_name_plural = "register entries"
        indexes = [
            models.Index(fields=["version", "plan_sku", "plan_version", "destination"]),
        ]

    def __str__(self):
        return f"{self.plan_sku or 'general'} {self.plan_version} · {self.territory_code}"


class PlanTariffMap(models.Model):
    """Which register SKU/version a line on `plan` + `term` is billed under.

    A line keeps the version that was in effect when it was activated, so
    historic customers aren't silently moved onto today's tariff.
    """

    plan = models.ForeignKey("plans.Plan", on_delete=models.CASCADE, related_name="roaming_tariff_maps")
    term = models.CharField(max_length=3, choices=TERM_CHOICES, blank=True, default="")
    plan_sku = models.CharField(max_length=64)
    plan_version = models.CharField(max_length=32, blank=True, default="")
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["plan", "term", "-effective_from"]

    def __str__(self):
        return f"{self.plan} {self.term or 'any'} → {self.plan_sku} {self.plan_version}"


class RoamingQuote(models.Model):
    """Immutable snapshot of a personalised quote, for audit and support."""

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    line = models.ForeignKey("add_line.Line", on_delete=models.SET_NULL, null=True, related_name="roaming_quotes")
    destination = models.ForeignKey(Destination, on_delete=models.SET_NULL, null=True, related_name="+")
    travel_date = models.DateField()
    register_version = models.ForeignKey(RegisterVersion, on_delete=models.SET_NULL, null=True, related_name="+")
    entry = models.ForeignKey(RegisterEntry, on_delete=models.SET_NULL, null=True, related_name="+")
    status = models.CharField(max_length=32, db_index=True)
    outcome = models.CharField(max_length=32, blank=True, default="")
    response = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.status} {self.outcome} · {self.destination_id} · {self.travel_date}"
