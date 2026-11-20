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
import re
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
        # Approved contract terms / tariff missing or unusable (Engineering
        # Decision v2.1 §04). Always internal: Legal/Product fix the content.
        CONTRACT_DOCS = "contract_docs", "Contract documents unavailable"
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
        ActionReason.CONTRACT_DOCS: ActionOwner.OPERATIONS,
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
    # The exact Step 4 sentence the customer ticked (blank on requests made
    # before 20 Nov 2026, when only the version was kept).
    consent_statement = models.TextField(blank=True, editable=False)
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


def _blank(value) -> bool:
    return not str(value if value is not None else "").strip()


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ApprovedContent(models.Model):
    """Where approved contract content came from, and whether it is live
    (Contract Recovery & Release decision v2.3 §1A and §5, 19 Nov 2026).

    Lifecycle, one way only. Each step is an admin action that needs its own
    permission; the state can't be typed in:

        DRAFT ──record approval──▶ APPROVED ──publish──▶ PUBLISHED ──retire──▶ RETIRED
          ▲                           │
          └──────return to draft──────┘   (only before it was ever used)

    * Recording approval needs evidence: the approval reference and date, the
      responsible owner and the signed master's file name and SHA-256. Ticking
      a box or editing a string is never approval, and a draft is never
      promoted automatically.
    * Only PUBLISHED content whose effective dates cover today is used for a
      customer's documents or shown on the website.
    * From APPROVED on, the record is frozen. A change is a new version;
      documents already issued keep their own copy either way.
    """

    class State(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved (evidence recorded, not live)"
        PUBLISHED = "published", "Published (in use)"
        RETIRED = "retired", "Retired"

    state = models.CharField(max_length=10, choices=State.choices, default=State.DRAFT, editable=False)

    # Evidence of the approval. That may be years old: this records it, it doesn't replace it.
    approval_reference = models.CharField(
        max_length=255, blank=True, help_text="Sign-off email, minutes or approval record reference.")
    approval_date = models.DateField(null=True, blank=True, help_text="Date the content was approved.")
    document_owner = models.CharField(max_length=150, blank=True, help_text="Responsible owner (name / role).")
    source_document = models.CharField(
        max_length=255, blank=True, help_text="File name of the signed / approved master document.")
    source_sha256 = models.CharField(
        max_length=64, blank=True, help_text="SHA-256 of that file. Filled in when you attach the file in admin.")
    source_location = models.CharField(
        max_length=255, blank=True, help_text="Where the controlled copy is kept (restricted repository).")

    effective_from = models.DateField(null=True, blank=True, help_text="First UK date this version may be used.")
    effective_to = models.DateField(null=True, blank=True, help_text="Not used from this date (set on retiring).")

    # Who recorded each step in this system, and when.
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        editable=False,
    )
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        editable=False,
    )
    published_at = models.DateTimeField(null=True, blank=True, editable=False)
    retired_at = models.DateTimeField(null=True, blank=True, editable=False)

    EVIDENCE_FIELDS = ("approval_reference", "approval_date", "document_owner", "source_document",
                       "source_sha256", "source_location", "effective_from")

    class Meta:
        abstract = True

    @property
    def frozen(self) -> bool:
        return self.state != self.State.DRAFT

    @property
    def is_live(self) -> bool:
        from django.utils import timezone

        today = timezone.localdate()
        return (self.state == self.State.PUBLISHED and self.effective_from is not None
                and self.effective_from <= today and (self.effective_to is None or self.effective_to > today))

    @classmethod
    def live(cls, today=None):
        """Published versions in effect on `today` (UK date), newest first."""
        from django.utils import timezone

        today = today or timezone.localdate()
        return (
            cls.objects.filter(state=cls.State.PUBLISHED, effective_from__lte=today)
            .filter(models.Q(effective_to__isnull=True) | models.Q(effective_to__gt=today))
            .order_by("-effective_from", "-published_at", "-pk")
        )

    def evidence_problems(self) -> list[str]:
        problems = [f for f in self.EVIDENCE_FIELDS if _blank(getattr(self, f))]
        if self.source_sha256 and not _SHA256_RE.match(self.source_sha256):
            problems.append("source_sha256 (must be 64 lower-case hex characters)")
        if self.effective_to and self.effective_from and self.effective_to <= self.effective_from:
            problems.append("effective_to (must be after effective_from)")
        return problems

    def content_problems(self) -> list[str]:
        raise NotImplementedError

    def approval_problems(self) -> list[str]:
        """Everything that stops this version being approved, for staff."""
        return self.evidence_problems() + self.content_problems()

    # ── Lifecycle (called from admin actions; each raises ValidationError) ──

    def record_approval(self, user):
        from django.utils import timezone

        if self.state != self.State.DRAFT:
            raise ValidationError("Only a draft can be approved.")
        problems = self.approval_problems()
        if problems:
            raise ValidationError(problems)
        self.state, self.approved_by, self.approved_at = self.State.APPROVED, user, timezone.now()
        self.save()

    def publish(self, user):
        from django.utils import timezone

        if self.state != self.State.APPROVED:
            raise ValidationError("Only approved content can be published.")
        problems = self.approval_problems()
        if problems:
            raise ValidationError(problems)
        self.state, self.published_by, self.published_at = self.State.PUBLISHED, user, timezone.now()
        self.save()

    def retire(self):
        from django.utils import timezone

        if self.state not in (self.State.APPROVED, self.State.PUBLISHED):
            raise ValidationError("Only approved or published content can be retired.")
        today = timezone.localdate()
        if self.effective_to is None or self.effective_to > today:
            self.effective_to = today
        self.state, self.retired_at = self.State.RETIRED, timezone.now()
        self.save()

    def return_to_draft(self):
        if self.state != self.State.APPROVED or self.published_at:
            raise ValidationError("Only approved content that was never published can go back to draft.")
        self.state, self.approved_by, self.approved_at = self.State.DRAFT, None, None
        self.save()


class ContractTerms(ApprovedContent):
    """Legal-approved wording used in every customer's Contract Summary and
    Contract Information. Only the newest live version is used, and each
    issued document stores a copy of what it used, so editing or replacing a
    version never changes documents already issued (CS-10, CI-17).
    """

    version = models.CharField(max_length=32, unique=True, help_text="e.g. 2026-11-v1")

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

    # Every field the documents print. All must be filled before approval.
    TEXT_FIELDS = (
        "legal_name", "company_number", "registered_office", "support_phone", "support_email", "website",
        "customer_care_hours", "complaints_route", "complaints_code", "adr_scheme", "compensation",
        "device", "service_availability", "quality_and_sla", "traffic_management", "performance_remedies",
        "payment_method", "billing_start", "usage_controls", "provisioning",
        "cancellation", "cooling_off", "prepaid_credit_refund",
        "data_categories", "privacy_policy", "security_incidents", "directory_listing",
        "emergency_access", "accessibility",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "contract terms (Legal)"
        verbose_name_plural = "contract terms (Legal)"
        permissions = [
            ("approve_contractterms", "Can record Legal approval of contract terms"),
            ("publish_contractterms", "Can publish or retire contract terms"),
        ]

    def __str__(self):
        return f"Contract terms {self.version} ({self.get_state_display()})"

    @classmethod
    def current(cls, today=None):
        return cls.live(today).first()

    def content_problems(self) -> list[str]:
        problems = [f for f in self.TEXT_FIELDS if _blank(getattr(self, f))]
        rules = list(self.term_rules.all()) if self.pk else []
        if not rules:
            problems.append("term rules (at least one plan length)")
        for rule in rules:
            problems += [f"rule {rule.term}.{f}" for f in ContractTermRule.TEXT_FIELDS if _blank(getattr(rule, f))]
        return problems


class ContractTermRule(models.Model):
    """Commitment, renewal and early-exit wording for one plan length."""

    terms = models.ForeignKey(ContractTerms, on_delete=models.CASCADE, related_name="term_rules")
    term = models.CharField(max_length=3, choices=Term.choices)
    minimum_commitment = _required_text("Length and the exact start trigger.")
    renewal = _required_text("What happens at the end of the commitment.")
    early_termination = _required_text("Exact charge or formula, or 'None' only if true.")

    TEXT_FIELDS = ("minimum_commitment", "renewal", "early_termination")

    class Meta:
        unique_together = [("terms", "term")]

    def __str__(self):
        return f"{self.terms.version} · {self.get_term_display()}"


# Ofcom (17 Jan 2025): new contracts can't have inflation-linked or
# percentage-based in-contract rises; any rise is stated in £/p with a date.
# Wording that looks like such a formula blocks approval (T03).
_FORMULA_PRICE_RISE_RE = re.compile(
    r"\d\s*(%|per\s*cent|percent)|\b(CPI|CPIH|RPI)\b|retail\s+prices?\s+index|consumer\s+prices?\s+index",
    re.IGNORECASE,
)


def formula_price_rise(text: str) -> bool:
    return bool(_FORMULA_PRICE_RISE_RE.search(text or ""))


class PlanTariff(ApprovedContent):
    """Product/Finance-approved tariff facts for one plan version (v2.3 §3, §5).

    A plan can have several versions over time; documents use the one live on
    the day they are issued.
    """

    plan = models.ForeignKey("plans.Plan", on_delete=models.CASCADE, related_name="contract_tariffs")
    version = models.CharField(max_length=32, help_text="Bump whenever the content changes.")

    # Commercial binding.
    plan_sku = models.CharField(max_length=64, blank=True, help_text="Plan SKU in the approved catalogue.")
    tariff_code = models.CharField(max_length=64, blank=True, help_text="Tariff / rate schedule code.")
    vat_basis = _required_text("Approved VAT statement, e.g. that all prices include VAT.")

    uk_data = _required_text("e.g. '30GB' or 'Unlimited (fair use applies)'.")
    uk_minutes = _required_text()
    uk_texts = _required_text()
    fair_use = _required_text("Material limits, speed caps, fair use; or 'None'.")
    after_allowance = _required_text(
        "Out-of-allowance rule for data, minutes and texts: charge (with VAT treatment), bar or slow down.")
    rollover_applies = models.BooleanField(
        null=True, blank=True,
        help_text="Does unused allowance carry over? Must be answered: blank is not 'No'.")
    rollover = _required_text("Approved rollover wording: eligible allowances, limits, expiry and exclusions.")
    roaming_zone = _required_text("Included destinations / zone (not '44 EU countries' as a shortcut).")
    roaming_allowance = _required_text("Included roaming data, minutes, texts and fair use.")
    roaming_rates = _required_text("Charges outside the zone or allowance; rate schedule version.")
    speeds = _required_text("Advertised and estimated maximum download / upload speeds.")
    extra_charges = _required_text("Out-of-allowance, international, premium: tariff version / link.")
    one_off_charges = _required_text("SIM / delivery / activation charges, or 'None'.")
    price_changes_apply = models.BooleanField(
        null=True, blank=True,
        help_text="Are there scheduled in-contract price rises? Yes needs the £/p rows below; must be answered.")
    scheduled_price_changes = _required_text(
        "Approved wording: either the approved 'no scheduled rises' statement, or the text introducing the "
        "dated £/p rises below. No CPI/RPI or percentage rises (Ofcom, 17 Jan 2025).")

    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    TEXT_FIELDS = (
        "plan_sku", "tariff_code", "vat_basis",
        "uk_data", "uk_minutes", "uk_texts", "fair_use", "after_allowance", "rollover",
        "roaming_zone", "roaming_allowance", "roaming_rates", "speeds", "extra_charges",
        "one_off_charges", "scheduled_price_changes",
    )

    class Meta:
        ordering = ["plan_id", "-effective_from", "-pk"]
        unique_together = [("plan", "version")]
        permissions = [
            ("approve_plantariff", "Can record Product/Finance approval of plan tariffs"),
            ("publish_plantariff", "Can publish or retire plan tariffs"),
        ]

    def __str__(self):
        return f"{self.plan} · tariff {self.version} ({self.get_state_display()})"

    @classmethod
    def current(cls, plan_id, today=None):
        return cls.live(today).filter(plan_id=plan_id).first()

    def content_problems(self) -> list[str]:
        problems = [f for f in self.TEXT_FIELDS if _blank(getattr(self, f))]
        if self.rollover_applies is None:
            problems.append("rollover_applies (answer yes or no)")
        if self.price_changes_apply is None:
            problems.append("price_changes_apply (answer yes or no)")
        if formula_price_rise(self.scheduled_price_changes):
            problems.append("scheduled_price_changes (CPI/RPI or percentage rises aren't allowed; use £/p and dates)")
        changes = list(self.price_changes.all()) if self.pk else []
        if self.price_changes_apply and not changes:
            problems.append("price changes (add the dated £/p rises, or answer No)")
        if self.price_changes_apply is False and changes:
            problems.append("price changes (rows entered but 'price changes apply' is No)")
        for change in changes:
            problems += [f"price change {change}: {p}" for p in change.problems(self.effective_from)]
        return problems


class PlanPriceChange(models.Model):
    """One scheduled in-contract price rise, in pounds and pence with a date."""

    tariff = models.ForeignKey(PlanTariff, on_delete=models.CASCADE, related_name="price_changes")
    term = models.CharField(max_length=3, choices=Term.choices, blank=True,
                            help_text="Plan length it applies to; blank = every plan length.")
    effective_date = models.DateField(help_text="Date the new price applies from.")
    new_price = models.DecimalField(max_digits=8, decimal_places=2,
                                    help_text="New recurring price in £, including VAT.")

    class Meta:
        ordering = ["effective_date", "term"]
        unique_together = [("tariff", "term", "effective_date")]

    def __str__(self):
        return f"{self.get_term_display() or 'all lengths'} from {self.effective_date}"

    def problems(self, effective_from=None) -> list[str]:
        out = []
        if self.new_price is None or self.new_price <= 0:
            out.append("new price must be more than £0.00")
        if effective_from and self.effective_date and self.effective_date <= effective_from:
            out.append("date must be after the tariff's effective_from")
        return out


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


class SwitchContractProvision(models.Model):
    """Each time contract documents were provided to the customer: made
    available in their account, emailed (with the mail service's answer) or
    downloaded. Append-only evidence that both documents were provided before
    consent (v2.3 §4–5, T04). Opening a PDF is never required to agree."""

    class Method(models.TextChoices):
        ACCOUNT = "account", "Made available to view and download in the customer's account"
        EMAIL = "email", "Emailed as PDF attachments"
        DOWNLOAD = "download", "Downloaded by the customer"

    pack = models.ForeignKey(SwitchContractPack, on_delete=models.PROTECT, related_name="provisions")
    # Blank = every document in the pack (account / email); set for a download.
    document = models.ForeignKey(
        SwitchContractDocument, on_delete=models.PROTECT, null=True, blank=True, related_name="provisions",
    )
    method = models.CharField(max_length=10, choices=Method.choices)
    succeeded = models.BooleanField()
    recipient_masked = models.CharField(max_length=255, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.pack.reference} · {self.get_method_display()}{'' if self.succeeded else ' (failed)'}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecord("Provision evidence can't be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecord("Provision evidence can't be deleted.")

    def as_evidence(self) -> dict:
        return {
            "method": self.method,
            "document": str(self.document.public_id) if self.document_id else None,
            "succeeded": self.succeeded,
            "at": self.created_at.isoformat(),
        }


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
    # Copy of the SwitchContractProvision events before consent: how and when
    # the documents were provided (T04).
    provision = models.JSONField(default=list)
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
