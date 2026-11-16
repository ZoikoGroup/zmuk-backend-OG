"""Switch & Save: a customer's request to move to Zoiko Mobile.

Follows the CTO Milestone 04 decision (12 Nov 2026, option (c)):

* Step 4 is a request, not a purchase: no payment and no account needed.
* The request has its own state machine (RECEIVED → VALIDATING →
  ACTION_NEEDED / READY_FOR_ORDER → CONVERTED). The commercial order, payment,
  SIM/line and number transfer are separate records (`order`, `line` and its
  port requests) and are the only source of the customer tracker's progress.
  A request status never claims an order, activation or transfer.
* PAC/STAC codes follow the add_line rules (decision C13): stored only
  Fernet-encrypted in `encrypted_code`, never returned by the API. Deleting
  them (`purge_switch_codes`) is off until retention is approved.
* Engineering note (13 Nov 2026): "action needed" is split between the
  customer and Operations (`action_owner`); only the customer's is shown.
"""

import hashlib
import secrets
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.add_line.models import SimType, Term

_REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
REFERENCE_PREFIX = "ZM-SW-"


def generate_reference() -> str:
    """Random, non-sequential reference, so it can't be guessed for lookups."""
    return REFERENCE_PREFIX + "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(8))


class SwitchRequest(models.Model):
    """A customer request to switch (port) their number to Zoiko Mobile."""

    class Status(models.TextChoices):
        RECEIVED = "received", "Request received"
        VALIDATING = "validating", "Checking your request"
        ACTION_NEEDED = "action_needed", "Action needed"
        READY_FOR_ORDER = "ready_for_order", "Ready for your order"
        CONVERTED = "converted", "Order created"
        CANCELLED = "cancelled", "Cancelled"
        # Staff only. No automatic expiry until Operations and Legal approve
        # the trigger, reminders and retention (note of 13 Nov 2026). Expiry
        # never deletes personal data.
        EXPIRED = "expired", "Expired"
        REJECTED = "rejected", "Unable to proceed"

    # The request itself is finished; any further progress lives on the order.
    TERMINAL_STATUSES = (Status.CANCELLED, Status.EXPIRED, Status.REJECTED)
    # No more customer input (codes, edits) on the request itself.
    CLOSED_STATUSES = TERMINAL_STATUSES + (Status.CONVERTED,)

    class ActionReason(models.TextChoices):
        CODE_NEEDED = "code_needed", "Switching code needed"
        CODE_INVALID = "code_invalid", "Switching code not accepted"
        CODE_EXPIRED = "code_expired", "Switching code expired"
        ELIGIBILITY = "eligibility", "Eligibility check"
        IDENTITY = "identity", "Identity / ownership check"
        DUPLICATE = "duplicate", "Possible duplicate request"
        OTHER = "other", "Other"

    # Reasons the customer resolves by sending a (new) PAC/STAC.
    CODE_REASONS = (ActionReason.CODE_NEEDED, ActionReason.CODE_INVALID, ActionReason.CODE_EXPIRED)

    class ActionOwner(models.TextChoices):
        """Who has to act (engineering note 13 Nov 2026). Only customer actions
        are shown to the customer as "Action needed"; an operations action is
        shown as "Checking your request"."""
        CUSTOMER = "customer", "Customer"
        OPERATIONS = "operations", "Operations"

    # Used when staff don't pick an owner. Code reasons are always the customer's.
    DEFAULT_ACTION_OWNER = {
        ActionReason.CODE_NEEDED: ActionOwner.CUSTOMER,
        ActionReason.CODE_INVALID: ActionOwner.CUSTOMER,
        ActionReason.CODE_EXPIRED: ActionOwner.CUSTOMER,
        ActionReason.ELIGIBILITY: ActionOwner.OPERATIONS,
        ActionReason.IDENTITY: ActionOwner.OPERATIONS,
        ActionReason.DUPLICATE: ActionOwner.OPERATIONS,
        ActionReason.OTHER: ActionOwner.OPERATIONS,
    }

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
    # Client-generated key (Idempotency-Key header): a retried submit returns
    # the original request instead of creating a second one.
    idempotency_key = models.CharField(max_length=64, null=True, blank=True, unique=True, editable=False)
    # The typed email matches an existing account. Recorded for ops/Milestone 4
    # only; it never links the request and is never shown to the submitter.
    existing_account_email = models.BooleanField(default=False, editable=False)

    # ── Step 1: Your Details ──────────────────────────────────────────────
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField()
    # Optional contact number ("only used for updates about your order").
    # The number being moved is `port_number`.
    mobile = models.CharField(max_length=20, blank=True)
    # Billing address; also the delivery address for a physical SIM. Required
    # by the API; blank only on old rows that had one free-text field.
    address_line_1 = models.CharField(max_length=255, blank=True)
    address_line_2 = models.CharField(max_length=255, blank=True)
    town_city = models.CharField(max_length=100, blank=True)
    # UK postcode, stored as "SW1A 1AA".
    postcode = models.CharField(max_length=10, blank=True)

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
    # Price shown at submit (G2): the quote, contract versions and its hash.
    # Milestone 4 re-quotes and asks for fresh acceptance if it changed.
    quote_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    quote_hash = models.CharField(max_length=64, blank=True, editable=False)
    # Contract Summary / Information sent to the customer (G3). Not at Step 4.
    precontract_documents_sent_at = models.DateTimeField(null=True, blank=True, editable=False)

    # ── Step 4: Number transfer ───────────────────────────────────────────
    switch_route = models.CharField(max_length=10, choices=Route.choices, blank=True)
    # The number being moved, as 447XXXXXXXXX (Transatel format).
    port_number = models.CharField(max_length=24, blank=True)
    pac_later = models.BooleanField(default=False)
    code_type = models.CharField(max_length=4, choices=CodeType.choices, blank=True)
    encrypted_code = models.TextField(blank=True)
    code_received_at = models.DateTimeField(null=True, blank=True)
    code_purged_at = models.DateTimeField(null=True, blank=True)

    # ── Authorisation and consent evidence (G4) ───────────────────────────
    transfer_authorised = models.BooleanField(default=False)
    timeline_acknowledged = models.BooleanField(default=False)
    terms_accepted = models.BooleanField(default=False)
    consent_version = models.CharField(max_length=32, blank=True, editable=False)
    consented_at = models.DateTimeField(null=True, blank=True, editable=False)
    consent_ip = models.GenericIPAddressField(null=True, blank=True, editable=False)
    consent_user_agent = models.CharField(max_length=255, blank=True, editable=False)
    # Marketing permissions are separate from service consent (never pre-ticked).
    opt_in_news = models.BooleanField(default=False)
    opt_in_texts = models.BooleanField(default=False)

    # ── Processing ────────────────────────────────────────────────────────
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.RECEIVED, db_index=True
    )
    action_reason = models.CharField(max_length=20, choices=ActionReason.choices, blank=True)
    # Set only while ACTION_NEEDED; defaults from the reason (DEFAULT_ACTION_OWNER).
    action_owner = models.CharField(max_length=12, choices=ActionOwner.choices, blank=True)
    # Internal only; never returned to the customer.
    ops_note = models.TextField(blank=True)
    # Filled in by Milestone 4 order conversion (not at submit).
    order = models.OneToOneField(
        "add_line.AddLineOrder", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="switch_request",
    )
    line = models.OneToOneField(
        "add_line.Line", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="switch_request",
    )
    # A switching date confirmed by ops. Never a calculated guess; the
    # supplier's port date takes over once a transfer is scheduled.
    confirmed_switch_date = models.DateField(null=True, blank=True)
    converted_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Milestone 4: when a signed-in customer took ownership through a
    # continuation link (proving they can read the request's email).
    claimed_at = models.DateTimeField(null=True, blank=True, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Switch Request"
        verbose_name_plural = "Switch Requests"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._loaded_status = None
        self._loaded_action = None
        # Optionally set by callers so history records who changed the status.
        self._status_actor = None

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_status = instance.__dict__.get("status")
        instance._loaded_action = (instance.__dict__.get("action_reason"), instance.__dict__.get("action_owner"))
        return instance

    def __str__(self):
        return f"{self.reference} · {self.first_name} {self.last_name} → {self.selected_plan} ({self.status})"

    @property
    def required_code_type(self):
        return self.ROUTE_CODE.get(self.switch_route)

    def clean(self):
        super().clean()
        if self.status == self.Status.ACTION_NEEDED and not self.action_reason:
            raise ValidationError({"action_reason": "Say what action is needed."})
        if (self.status == self.Status.ACTION_NEEDED and self.action_reason in self.CODE_REASONS
                and self.action_owner == self.ActionOwner.OPERATIONS):
            raise ValidationError({"action_owner": "Only the customer can send a switching code."})
        # Checked on the move only, so legacy rows (migration 0006) stay editable.
        moving_to_converted = self.status == self.Status.CONVERTED and self._loaded_status != self.Status.CONVERTED
        if moving_to_converted and not self.order_id:
            raise ValidationError({"status": "A request is only converted once an order is linked."})

    def save(self, *args, **kwargs):
        from django.utils import timezone

        creating = self._state.adding
        if not self.reference:
            self.reference = generate_reference()
            while SwitchRequest.objects.filter(reference=self.reference).exists():
                self.reference = generate_reference()
        status_changed = (
            not creating and self._loaded_status is not None and self.status != self._loaded_status
        )
        extra_fields = []
        if self.status != self.Status.ACTION_NEEDED:
            if self.action_reason:
                self.action_reason = ""
                extra_fields.append("action_reason")
            if self.action_owner:
                self.action_owner = ""
                extra_fields.append("action_owner")
        elif self.action_reason in self.CODE_REASONS and self.action_owner != self.ActionOwner.CUSTOMER:
            self.action_owner = self.ActionOwner.CUSTOMER
            extra_fields.append("action_owner")
        elif not self.action_owner:
            self.action_owner = self.DEFAULT_ACTION_OWNER.get(self.action_reason, self.ActionOwner.OPERATIONS)
            extra_fields.append("action_owner")
        if self.status == self.Status.CONVERTED and not self.converted_at:
            self.converted_at = timezone.now()
            extra_fields.append("converted_at")
        if extra_fields and kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = list(dict.fromkeys(list(kwargs["update_fields"]) + extra_fields))

        super().save(*args, **kwargs)

        # A change of reason/owner while still ACTION_NEEDED is history too.
        action_changed = (
            not creating and not status_changed and self.status == self.Status.ACTION_NEEDED
            and (self.action_reason, self.action_owner) != self._loaded_action
        )
        if creating or status_changed or action_changed:
            SwitchStatusEvent.objects.create(
                switch_request=self,
                kind=SwitchStatusEvent.Kind.CREATED if creating else SwitchStatusEvent.Kind.STATUS_CHANGED,
                status_before="" if creating else self._loaded_status,
                status_after=self.status,
                action_reason=self.action_reason,
                action_owner=self.action_owner,
                actor=self._status_actor,
            )
        self._loaded_status = self.status
        self._loaded_action = (self.action_reason, self.action_owner)

    def set_status(self, status, *, reason="", owner="", actor=None):
        """Change status (reason, owner) and save; history is recorded by save().

        `owner` defaults from the reason (DEFAULT_ACTION_OWNER).
        """
        needed = status == self.Status.ACTION_NEEDED
        self.status = status
        self.action_reason = reason if needed else ""
        self.action_owner = (owner or self.DEFAULT_ACTION_OWNER.get(reason, self.ActionOwner.OPERATIONS)) if needed else ""
        self._status_actor = actor if actor is not None and getattr(actor, "is_authenticated", False) else None
        self.save(update_fields=["status", "action_reason", "action_owner", "updated_at"])

    @property
    def needs_customer(self) -> bool:
        return self.status == self.Status.ACTION_NEEDED and self.action_owner == self.ActionOwner.CUSTOMER


class SwitchStatusEvent(models.Model):
    """Append-only history of a switch request, shown to the customer."""

    class Kind(models.TextChoices):
        CREATED = "created", "Request received"
        STATUS_CHANGED = "status_changed", "Status changed"
        CODE_RECEIVED = "code_received", "Switching code received"
        LINK_SENT = "link_sent", "Continuation link sent"
        CLAIMED = "claimed", "Linked to account"
        CONSENTED = "consented", "Contract agreed"
        CHECKOUT_STARTED = "checkout_started", "Checkout started"
        CODE_TRANSFERRED = "code_transferred", "Code handed to number transfer"

    switch_request = models.ForeignKey(
        SwitchRequest, on_delete=models.CASCADE, related_name="events"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status_before = models.CharField(max_length=20, blank=True)
    status_after = models.CharField(max_length=20, blank=True)
    action_reason = models.CharField(max_length=20, blank=True)
    action_owner = models.CharField(max_length=12, blank=True)
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


class SwitchContinuationToken(models.Model):
    """Single-use link emailed to the request's address (Milestone 4).

    Opening it proves the customer can read that inbox, which is what makes
    it safe to attach the request to the account they sign in with (never
    email matching alone, decision A1). Only a SHA-256 of the token is
    stored, so a database leak gives no working links.
    """

    switch_request = models.ForeignKey(
        SwitchRequest, on_delete=models.CASCADE, related_name="continuation_tokens"
    )
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    # Masked address the link went to (for support), and whether the mail
    # backend accepted it.
    sent_to = models.CharField(max_length=255, blank=True)
    delivered = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    used_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    # Set when a newer link replaces this one.
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.switch_request.reference} · link {self.created_at:%d %b %Y %H:%M}"


# ── Milestone 4 · B2: contract documents (CTO docs 01 and 02, v1.0) ──────────

def _required_text(help_text=""):
    # Blank is allowed while drafting; document generation refuses to issue
    # anything while a field is blank (never "TBC", a default or £0.00).
    return models.TextField(blank=True, help_text=help_text)


class ContractTerms(models.Model):
    """Legal-approved wording used in every customer's Contract Summary and
    Contract Information. Only the newest *approved* version is used, and each
    issued document stores a copy of what it used, so editing or replacing a
    version never changes documents already issued (CS-10, CI-17).
    """

    version = models.CharField(max_length=32, unique=True, help_text="e.g. 2026-11-v1")
    approved = models.BooleanField(default=False, help_text="Legal / Compliance sign-off given.")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    # Issuer (Companies House: ZOIKO MOBILE UK LTD, 16564980).
    legal_name = models.CharField(max_length=120, default="Zoiko Mobile UK Ltd")
    company_number = models.CharField(max_length=20, default="16564980")
    registered_office = models.CharField(
        max_length=255, default="167–169 Great Portland Street, 5th Floor, London W1W 5PF")
    support_phone = models.CharField(max_length=40, default="020 7164 6399")
    support_email = models.EmailField(default="info@zoikomobile.co.uk")
    website = models.CharField(max_length=120, default="zoikomobile.co.uk")

    # Support, complaints and redress (CI §01, §10).
    customer_care_hours = _required_text("Contact channels and opening hours.")
    complaints_route = _required_text("How to complain, response and escalation.")
    complaints_code = _required_text("Complaints Code version and accessible link.")
    adr_scheme = _required_text("Ofcom-approved ADR scheme Zoiko is a member of, how to contact it.")
    compensation = _required_text("Remedies for delayed/failed switching, service and billing problems.")

    # Service (CI §02–03, CS speeds/remedies).
    device = _required_text("e.g. 'No device is supplied with this plan.'")
    service_availability = _required_text("Approved coverage / 4G / 5G wording.")
    quality_and_sla = _required_text("Minimum quality level or 'no minimum level offered'.")
    traffic_management = _required_text("Traffic management and congestion policy.")
    performance_remedies = _required_text("Remedies for material or recurring performance problems.")

    # Charges, billing and delivery (CI §03, §05, §06).
    payment_method = _required_text("Permitted payment method(s) and collection timing.")
    billing_start = _required_text("When billing starts.")
    usage_controls = _required_text("How to see usage and bills; spend controls.")
    provisioning = _required_text("SIM delivery / eSIM steps and expected service start.")

    # Ending the agreement (CI §07).
    cancellation = _required_text("Cancelling or moving provider: channels, notice, timing.")
    cooling_off = _required_text("Distance-selling cancellation right and exceptions.")
    prepaid_credit_refund = _required_text("Refund of remaining credit on switching, or 'Not applicable'.")

    # Rights (CI §08–09).
    data_categories = _required_text("Personal data used to provide the service.")
    privacy_policy = _required_text("Privacy policy version and link.")
    security_incidents = _required_text("Actions Zoiko may take on security incidents.")
    directory_listing = _required_text("Directory listing choice, or 'Not applicable'.")
    emergency_access = _required_text("999/112 access and caller location statement.")
    accessibility = _required_text("Accessible features and how to request accessible formats.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "contract terms (Legal)"
        verbose_name_plural = "contract terms (Legal)"

    def __str__(self):
        return f"Contract terms {self.version}{' (approved)' if self.approved else ''}"

    @classmethod
    def current(cls):
        return cls.objects.filter(approved=True).order_by("-approved_at", "-pk").first()


class ContractTermRule(models.Model):
    """Commitment, renewal and early-exit wording for one plan length."""

    terms = models.ForeignKey(ContractTerms, on_delete=models.CASCADE, related_name="term_rules")
    term = models.CharField(max_length=3, choices=Term.choices)
    minimum_commitment = _required_text("Length and the exact start trigger.")
    renewal = _required_text("What happens at the end of the commitment.")
    early_termination = _required_text("Exact charge or formula, or 'None' only if true.")

    class Meta:
        unique_together = [("terms", "term")]

    def __str__(self):
        return f"{self.terms.version} · {self.get_term_display()}"


class PlanTariff(models.Model):
    """Product-approved tariff facts for one plan, used in its documents."""

    plan = models.OneToOneField("plans.Plan", on_delete=models.CASCADE, related_name="contract_tariff")
    version = models.CharField(max_length=32, help_text="Bump whenever the content changes.")
    approved = models.BooleanField(default=False, help_text="Commercial / Product sign-off given.")
    approved_at = models.DateTimeField(null=True, blank=True)

    uk_data = _required_text("e.g. '30GB' or 'Unlimited (fair use applies)'.")
    uk_minutes = _required_text()
    uk_texts = _required_text()
    fair_use = _required_text("Material limits, speed caps, fair use; or 'None'.")
    after_allowance = _required_text("What happens after the allowance (charge, block, slow down).")
    rollover = _required_text("Whether unused allowance carries over.")
    roaming_zone = _required_text("Included destinations / zone (not '44 EU countries' as a shortcut).")
    roaming_allowance = _required_text("Included roaming data, minutes, texts and fair use.")
    roaming_rates = _required_text("Charges outside the zone or allowance; rate schedule version.")
    speeds = _required_text("Advertised and estimated maximum download / upload speeds.")
    extra_charges = _required_text("Out-of-allowance, international, premium: tariff version / link.")
    one_off_charges = _required_text("SIM / delivery / activation charges, or 'None'.")
    scheduled_price_changes = _required_text("Fixed future price changes with dates, or 'None'.")

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.plan} · tariff {self.version}{' (approved)' if self.approved else ''}"


class ImmutableRecord(Exception):
    pass


class SwitchContractPack(models.Model):
    """The documents issued to one customer from one frozen quote.

    Created before consent (CS/CI timing rule). Its quote, content snapshot
    and PDFs never change; a new quote means a new pack. B3 records consent
    on the pack; payment is only allowed against a consented pack.
    """

    class EmailStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Accepted by mail service"
        FAILED = "failed", "Failed"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    reference = models.CharField(max_length=24, unique=True, editable=False)
    switch_request = models.ForeignKey(SwitchRequest, on_delete=models.PROTECT, related_name="contract_packs")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    quote = models.JSONField(editable=False)
    quote_hash = models.CharField(max_length=64, editable=False)
    terms_version = models.CharField(max_length=32, editable=False)
    tariff_version = models.CharField(max_length=32, editable=False)
    # Every value printed in the PDFs, as issued.
    content = models.JSONField(editable=False)
    offer_valid_until = models.DateTimeField(editable=False)

    email_status = models.CharField(max_length=10, choices=EmailStatus.choices, default=EmailStatus.PENDING)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    # Set when a newer pack replaces this one before consent.
    superseded_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} · {self.switch_request.reference}"

    @staticmethod
    def new_reference() -> str:
        return "ZM-CQ-" + "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(10))

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = self.new_reference()
        super().save(*args, **kwargs)


class SwitchContractDocument(models.Model):
    """One immutable PDF. The bytes and their SHA-256 never change."""

    class Kind(models.TextChoices):
        CONTRACT_SUMMARY = "contract_summary", "Contract summary"
        CONTRACT_INFORMATION = "contract_information", "Contract information"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    pack = models.ForeignKey(SwitchContractPack, on_delete=models.PROTECT, related_name="documents")
    kind = models.CharField(max_length=24, choices=Kind.choices)
    filename = models.CharField(max_length=120)
    pdf = models.BinaryField(editable=False)
    sha256 = models.CharField(max_length=64, editable=False)
    size = models.PositiveIntegerField(editable=False)
    template_version = models.CharField(max_length=32, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("pack", "kind")]
        ordering = ["pack", "kind"]

    def __str__(self):
        return f"{self.get_kind_display()} · {self.pack.reference}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecord("Issued contract documents can't be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecord("Issued contract documents can't be deleted.")

    def bytes_intact(self) -> bool:
        return hashlib.sha256(bytes(self.pdf)).hexdigest() == self.sha256


class SwitchContractConsent(models.Model):
    """Express consent to one issued pack (CS-02, CI-03), recorded only after
    the customer was given both documents. Evidence: never edited; only the
    order it led to is filled in afterwards."""

    pack = models.OneToOneField(SwitchContractPack, on_delete=models.PROTECT, related_name="consent")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    # The exact wording the customer ticked, and its version.
    statement = models.TextField()
    statement_version = models.CharField(max_length=32)
    # {document public id: sha256} for both documents, as confirmed by the customer.
    documents = models.JSONField()
    immediate_service_requested = models.BooleanField(default=False)
    consented_at = models.DateTimeField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    order = models.OneToOneField(
        "add_line.AddLineOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="switch_consent",
    )

    def __str__(self):
        return f"Consent · {self.pack.reference}"

    def save(self, *args, **kwargs):
        if not self._state.adding and set(kwargs.get("update_fields") or ()) != {"order"}:
            raise ImmutableRecord("Consent evidence can't be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecord("Consent evidence can't be deleted.")


# ── Milestone 4 · B4: payment receipts (CTO document 03, v1.0) ───────────────

class SwitchPaymentCapture(models.Model):
    """One verified, captured payment from the provider (Stripe) for a
    switch order. Money actually taken, not the catalogue price. A receipt
    is issued only from one of these (PR-01..PR-06)."""

    class Status(models.TextChoices):
        CAPTURED = "captured", "Captured"
        PARTIALLY_REFUNDED = "partially_refunded", "Partially refunded"
        REFUNDED = "refunded", "Refunded"
        DISPUTED = "disputed", "Disputed"

    class ReceiptStatus(models.TextChoices):
        PENDING = "pending", "Preparing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed (retrying)"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    switch_request = models.ForeignKey(SwitchRequest, on_delete=models.PROTECT, related_name="payment_captures")
    order = models.ForeignKey("add_line.AddLineOrder", on_delete=models.PROTECT, related_name="switch_captures")
    provider = models.CharField(max_length=20, default="stripe", editable=False)
    payment_intent_id = models.CharField(max_length=255, editable=False)
    # Stripe charge id; one capture per id, so replayed events can't duplicate (PR-07).
    provider_capture_id = models.CharField(max_length=255, editable=False)
    amount_minor = models.PositiveIntegerField(editable=False)
    currency = models.CharField(max_length=3, editable=False)
    captured_at = models.DateTimeField(editable=False)
    # e.g. "Visa ending 4242"; never more than the last four digits.
    method_label = models.CharField(max_length=60, blank=True, editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CAPTURED)
    refunded_minor = models.PositiveIntegerField(default=0)
    receipt_status = models.CharField(max_length=10, choices=ReceiptStatus.choices, default=ReceiptStatus.PENDING)
    receipt_attempts = models.PositiveSmallIntegerField(default=0)
    receipt_error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-captured_at"]
        unique_together = [("provider", "provider_capture_id")]

    def __str__(self):
        return f"{self.switch_request.reference} · £{self.amount_minor / 100:.2f} {self.get_status_display()}"


class SwitchPaymentReceipt(models.Model):
    """The immutable receipt PDF for one capture (one capture → one receipt)."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    capture = models.OneToOneField(SwitchPaymentCapture, on_delete=models.PROTECT, related_name="receipt")
    receipt_number = models.CharField(max_length=24, unique=True, editable=False)
    filename = models.CharField(max_length=120)
    pdf = models.BinaryField(editable=False)
    sha256 = models.CharField(max_length=64, editable=False)
    size = models.PositiveIntegerField(editable=False)
    template_version = models.CharField(max_length=32, editable=False)
    issued_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.receipt_number

    @staticmethod
    def new_number() -> str:
        return "ZM-RC-" + "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(10))

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecord("Issued receipts can't be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecord("Issued receipts can't be deleted.")

    def bytes_intact(self) -> bool:
        return hashlib.sha256(bytes(self.pdf)).hexdigest() == self.sha256


class SwitchPaymentAdjustment(models.Model):
    """A refund or dispute on a capture. The original receipt is never
    changed; this is the linked later record (PR-16, PR-17)."""

    class Kind(models.TextChoices):
        REFUND = "refund", "Refund"
        DISPUTE = "dispute", "Dispute / chargeback"

    capture = models.ForeignKey(SwitchPaymentCapture, on_delete=models.PROTECT, related_name="adjustments")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    # Stripe event id: a replayed event is ignored.
    provider_event_id = models.CharField(max_length=255, unique=True)
    amount_minor = models.PositiveIntegerField()
    provider_status = models.CharField(max_length=40, blank=True)
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["occurred_at"]


class SwitchDocumentDelivery(models.Model):
    """Every attempt to email a receipt, first send or staff re-send. The
    customer is only told "sent" when the mail service accepted it (PR-27)."""

    receipt = models.ForeignKey(SwitchPaymentReceipt, on_delete=models.PROTECT, related_name="deliveries")
    recipient_masked = models.CharField(max_length=255)
    accepted = models.BooleanField()
    resend = models.BooleanField(default=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


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
