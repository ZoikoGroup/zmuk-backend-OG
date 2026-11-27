"""Data model for Change SIM type (Closing Task 05, physical SIM <-> eSIM).

Follows the CTO Engineering Response v2.0 and Decision Addendum v2.1
(both 26 Nov 2026; v2.1 takes precedence where they differ):

* `SimTypeChangeCase` is one request to move a line to the other SIM type.
  The number, plan, add-ons, allowance and billing stay the same; only the SIM
  changes, and only after the supplier confirms it (v2.0 §4.1).
* One primary lifecycle (`status`) for both directions. Physical delivery and
  eSIM installation are tracked separately in `physical_fulfilment` and
  `esim_fulfilment` (v2.1 B1). Supplier binding, installation access and
  handset connectivity are separate milestones.
* State changes go through `services.transitions`, never by assigning a
  status. Every change bumps `version` and writes an append-only
  `SimTypeChangeEvent`.
* `ProviderCommand` is one supplier action (SIM_CARD_SWAP only — never
  new-subscriber activation, MSISDN swap or porting). In Phase 1 it is the
  Network Ops work item: authorised by one person, executed in Auriga by an
  authorised executor, never by the person who verified the customer (v2.1 A3).
* `SupplierVerificationEvidence` is the fresh supplier read that proves the
  new SIM is bound; it is the only thing that can confirm a swap.
* `StepUpGrant` is the recorded identity verification for one case and one
  purpose (v2.1 A3 contract). Password re-entry alone never creates one.
* `LineOperationLock` stops a SIM change, a port, a suspension or a Task 04
  replacement running on the same line at once.
* `SwapSimStock` is the dedicated, supplier-recognised replacement stock
  (v2.1 B4); Sales SIMs never enter it.
* Every row carries the server-controlled tenant (v2.1 B2).

Sensitive identifiers are stored as keyed HMAC fingerprints or encrypted
text, never in plain columns. Nothing here talks to Transatel.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.add_line.models import SimType


def current_tenant() -> str:
    """Tenant from server configuration, never from a request (v2.1 B2)."""
    return getattr(settings, "SIM_CHANGE_TENANT", "ZMUK")


def tenant_field():
    return models.CharField(max_length=16, default=current_tenant, editable=False, db_index=True)


class Direction(models.TextChoices):
    PSIM_TO_ESIM = "psim_to_esim", "Physical SIM to eSIM"
    ESIM_TO_PSIM = "esim_to_psim", "eSIM to physical SIM"

    @classmethod
    def for_types(cls, from_type: str, to_type: str) -> "Direction":
        if from_type == SimType.PSIM and to_type == SimType.ESIM:
            return cls.PSIM_TO_ESIM
        if from_type == SimType.ESIM and to_type == SimType.PSIM:
            return cls.ESIM_TO_PSIM
        raise ValueError(f"no SIM type change from {from_type!r} to {to_type!r}")


class StatusSource(models.TextChoices):
    CUSTOMER = "customer", "Customer"
    SUPPORT = "support", "Support / Network Ops"
    SYSTEM = "system", "System"
    SUPPLIER_EVENT = "supplier_event", "Supplier event"
    SUPPLIER_POLL = "supplier_poll", "Supplier read"


class ActionOwner(models.TextChoices):
    NONE = "", "Nobody"
    CUSTOMER = "customer", "Customer"
    SUPPORT = "support", "Support"
    NETWORK_OPS = "network_ops", "Network Ops"
    SECURITY = "security", "Security"
    SUPPLIER = "supplier", "Supplier"


class PhysicalFulfilment(models.TextChoices):
    """Physical SIM delivery (v2.1 B1). Blank = not started yet."""

    NOT_STARTED = "", "Not started"
    NOT_REQUIRED = "NOT_REQUIRED", "Not required"
    AWAITING_DISPATCH = "AWAITING_DISPATCH", "Awaiting dispatch"
    SHIPPED = "SHIPPED", "Shipped"
    DELIVERED = "DELIVERED", "Courier reports delivered"
    RECEIPT_CONFIRMED = "RECEIPT_CONFIRMED", "Customer confirmed receipt"
    EXCEPTION = "EXCEPTION", "Delivery exception"


class EsimFulfilment(models.TextChoices):
    """eSIM installation (v2.1 B1). Blank = not started yet."""

    NOT_STARTED = "", "Not started"
    NOT_REQUIRED = "NOT_REQUIRED", "Not required"
    PROFILE_AVAILABLE = "PROFILE_AVAILABLE", "Profile available at supplier"
    ACCESS_READY = "ACCESS_READY", "Secure access ready"
    ACCESS_ISSUED = "ACCESS_ISSUED", "Secure access issued"
    INSTALLATION_REPORTED = "INSTALLATION_REPORTED", "Customer reported installation"
    INSTALLATION_VERIFIED = "INSTALLATION_VERIFIED", "Installation verified"
    EXCEPTION = "EXCEPTION", "Installation exception"


class SimTypeChangeCase(models.Model):
    """One request to change a line's SIM type (v2.0 §6–§7, v2.1 B1)."""

    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", "Requested"
        ELIGIBILITY_PENDING = "ELIGIBILITY_PENDING", "Checking eligibility"
        VERIFY_REQUIRED = "VERIFY_REQUIRED", "Identity verification required"
        VERIFIED = "VERIFIED", "Identity verified"
        QUOTED = "QUOTED", "Quoted"
        AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION", "Awaiting customer confirmation"
        CASE_ACCEPTED = "CASE_ACCEPTED", "Request accepted"
        STOCK_RESERVED = "STOCK_RESERVED", "Replacement SIM reserved"
        PROVIDER_PENDING = "PROVIDER_PENDING", "Approved, waiting for supplier action"
        SWAP_IN_PROGRESS = "SWAP_IN_PROGRESS", "Swap in progress"
        SUPPLIER_BINDING_CONFIRMED = "SUPPLIER_BINDING_CONFIRMED", "Supplier confirmed new SIM"
        SERVICE_READY = "SERVICE_READY", "Service ready on new SIM"
        CLOSED = "CLOSED", "Closed"
        # Exceptions
        HOLD_FRAUD = "HOLD_FRAUD", "On hold: fraud review"
        HOLD_CUSTOMER = "HOLD_CUSTOMER", "On hold: waiting for customer"
        SUPPORT_REQUIRED = "SUPPORT_REQUIRED", "Support required"
        RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED", "Supplier status being checked"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    TERMINAL_STATUSES = (Status.CLOSED, Status.FAILED, Status.CANCELLED)
    HOLD_STATUSES = (
        Status.HOLD_FRAUD, Status.HOLD_CUSTOMER, Status.SUPPORT_REQUIRED, Status.RECONCILIATION_REQUIRED,
    )

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    tenant = tenant_field()
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sim_type_changes"
    )
    line = models.ForeignKey(
        "add_line.Line", on_delete=models.PROTECT, related_name="sim_type_changes"
    )
    direction = models.CharField(max_length=16, choices=Direction.choices)
    from_type = models.CharField(max_length=4, choices=SimType.choices)
    to_type = models.CharField(max_length=4, choices=SimType.choices)
    old_sim = models.ForeignKey(
        "sims.Sim", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    # Reserved Swap SIM; null until STOCK_RESERVED.
    new_sim = models.ForeignKey(
        "sims.Sim", on_delete=models.PROTECT, null=True, blank=True, related_name="sim_type_change_cases"
    )

    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.REQUESTED, db_index=True
    )
    # Bumped on every change; a stale writer's compare-and-set fails.
    version = models.PositiveIntegerField(default=1)
    status_source = models.CharField(max_length=16, choices=StatusSource.choices, default=StatusSource.CUSTOMER)
    action_owner = models.CharField(max_length=16, choices=ActionOwner.choices, blank=True, default="")
    # Where a hold came from, so it can resume to the same point.
    held_from_status = models.CharField(max_length=32, choices=Status.choices, blank=True, default="")

    # ── Separate fulfilment tracks (v2.1 B1) ──
    physical_fulfilment = models.CharField(
        max_length=20, choices=PhysicalFulfilment.choices, blank=True, default=PhysicalFulfilment.NOT_STARTED,
    )
    esim_fulfilment = models.CharField(
        max_length=24, choices=EsimFulfilment.choices, blank=True, default=EsimFulfilment.NOT_STARTED,
    )
    # Manual courier tracking for the assisted launch (v2.1 B5).
    carrier = models.CharField(max_length=64, blank=True, default="")
    tracking_reference = models.CharField(max_length=64, blank=True, default="")
    dispatched_at = models.DateTimeField(null=True, blank=True)
    dispatch_source = models.CharField(max_length=32, blank=True, default="")
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    # Redacted picture of what must not change: plan, term, line status and
    # fingerprints of MSISDN / old ICCID. No raw identifiers.
    before_snapshot = models.JSONField(default=dict, blank=True)
    risk_flags = models.JSONField(default=list, blank=True)
    fraud_case_reference = models.CharField(max_length=64, blank=True, default="")
    supplier_correlation_id = models.CharField(max_length=128, blank=True, default="", db_index=True)

    # ── Quote (no fee is collected until Commercial approves, v2.1 §4.5) ──
    quote = models.JSONField(default=dict, blank=True)
    quote_version = models.CharField(max_length=32, blank=True, default="")
    quote_hash = models.CharField(max_length=64, blank=True, default="")
    quote_expires_at = models.DateTimeField(null=True, blank=True)
    currency = models.CharField(max_length=3, default="GBP")
    fee_gross = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    vat_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    refund_state = models.CharField(max_length=16, blank=True, default="")
    payment_intent_id = models.CharField(max_length=255, blank=True, default="")

    # Physical SIM only: validated, structured UK delivery snapshot, encrypted
    # (services.address). The fingerprint lets us compare addresses without
    # decrypting them.
    class AddressSource(models.TextChoices):
        NONE = "", "Not set"
        ON_FILE = "on_file", "Address on file"
        NEW = "new", "New address (enhanced review)"

    delivery_snapshot_encrypted = models.TextField(blank=True, default="")
    delivery_address_fingerprint = models.CharField(max_length=64, blank=True, default="")
    delivery_address_source = models.CharField(
        max_length=8, choices=AddressSource.choices, blank=True, default=AddressSource.NONE,
    )
    # What the customer agreed to: wording version, time, IP, user agent.
    consent = models.JSONField(default=dict, blank=True)

    # Set when the swap was executed at the supplier (Auriga or API). After
    # this the case can no longer simply be cancelled.
    supplier_submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    binding_confirmed_at = models.DateTimeField(null=True, blank=True)
    service_ready_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "SIM type change case"
        # Staff steps (v2.1 A3). Grouped into roles by services.roles.
        permissions = [
            ("verify_customer", "Can record customer identity verification"),
            ("approve_verification", "Can approve a high-risk verification (second person)"),
            ("authorise_swap", "Can authorise a SIM card swap"),
            ("execute_swap", "Can record an executed Auriga SIM card swap"),
            ("manage_fulfilment", "Can record SIM dispatch and delivery"),
            ("manage_stock", "Can reserve and quarantine Swap SIMs"),
            ("review_fraud", "Can review fraud holds and delivery address reviews"),
            ("manage_cases", "Can cancel requests and release support holds"),
        ]
        constraints = [
            # One open case per line.
            models.UniqueConstraint(
                fields=["tenant", "line"],
                condition=~Q(status__in=["CLOSED", "FAILED", "CANCELLED"]),
                name="sim_change_one_open_case_per_line",
            ),
            # A reserved SIM backs at most one open case.
            models.UniqueConstraint(
                fields=["new_sim"],
                condition=Q(new_sim__isnull=False) & ~Q(status__in=["CLOSED", "FAILED", "CANCELLED"]),
                name="sim_change_one_open_case_per_new_sim",
            ),
            models.CheckConstraint(
                condition=~Q(from_type=models.F("to_type")),
                name="sim_change_types_differ",
            ),
        ]

    def __str__(self):
        return f"{self.uuid} · {self.get_direction_display()} · {self.status}"

    @property
    def is_open(self) -> bool:
        return self.status not in self.TERMINAL_STATUSES

    @property
    def supplier_submitted(self) -> bool:
        return self.supplier_submitted_at is not None


class SimTypeChangeEvent(models.Model):
    """Append-only audit trail for a case. Rows are never edited or deleted."""

    tenant = tenant_field()
    case = models.ForeignKey(SimTypeChangeCase, on_delete=models.PROTECT, related_name="events")
    # Null for system / supplier actions.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    actor_role = models.CharField(max_length=32, blank=True, default="")
    # status_change | physical_fulfilment | esim_fulfilment | case_opened | command_* …
    kind = models.CharField(max_length=48)
    status_before = models.CharField(max_length=32, blank=True, default="")
    status_after = models.CharField(max_length=32, blank=True, default="")
    status_source = models.CharField(max_length=16, choices=StatusSource.choices)
    version = models.PositiveIntegerField()
    # Redacted reason text only; no identifiers or secrets.
    reason = models.CharField(max_length=255, blank=True, default="")
    evidence = models.ForeignKey(
        "SupplierVerificationEvidence", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    detail = models.JSONField(default=dict, blank=True)
    trace_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.kind} {self.status_before}→{self.status_after} (v{self.version})"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("SIM type change events are append-only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("SIM type change events are append-only")


class StepUpGrant(models.Model):
    """Recorded identity verification for one case and one purpose (v2.1 A3).

    Phase 1: a Customer Security Support Agent performs the Security-approved
    independent check and records the outcome here. Single use, short-lived,
    bound to the exact case. No passwords, OTPs, document images or raw
    answers are stored.
    """

    class Purpose(models.TextChoices):
        SIM_TYPE_CHANGE = "sim_type_change", "SIM type change"
        ESIM_INSTALL_ACCESS = "esim_install_access", "eSIM install access"

    class Method(models.TextChoices):
        SUPERVISED_SUPPORT = "supervised_support", "Supervised independent verification"
        INDEPENDENT_MFA = "independent_mfa", "Independent MFA (Phase 2)"

    class Assurance(models.TextChoices):
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"
        REFERRED = "referred", "Referred to Security / Fraud"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    tenant = tenant_field()
    case = models.ForeignKey(SimTypeChangeCase, on_delete=models.CASCADE, related_name="step_up_grants")
    line = models.ForeignKey("add_line.Line", on_delete=models.CASCADE, related_name="step_up_grants")
    subject = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="step_up_grants")
    purpose = models.CharField(max_length=32, choices=Purpose.choices)
    verification_method = models.CharField(max_length=32, choices=Method.choices)
    assurance_result = models.CharField(max_length=16, choices=Assurance.choices)
    risk_tier = models.CharField(max_length=16, blank=True, default="")
    risk_decision_id = models.CharField(max_length=64, blank=True, default="")
    # Version of the Security-approved procedure the agent followed.
    policy_version = models.CharField(max_length=32)
    # External decision reference (call ID, ticket); no PII.
    external_reference = models.CharField(max_length=128, blank=True, default="")
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    # Second person; required for high-risk grants and overrides.
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    issued_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["-issued_at"]

    def __str__(self):
        return f"step-up {self.purpose} · case {self.case_id} · {self.assurance_result}"


class ProviderCommand(models.Model):
    """One supplier action (v2.0 §9.2, v2.1 A3).

    Phase 1 (manual Auriga): a Network Ops Authoriser approves the work item
    (`authorised_by`), then an Executor carries it out and records the
    supplier correlation (`executed_by`). Neither may be the person who
    verified the customer. A timeout after sending is UNKNOWN, never FAILED:
    reconcile first and never resend a swap blindly.
    """

    class Operation(models.TextChoices):
        SIM_CARD_SWAP = "SIM_CARD_SWAP", "SIM card swap"

    class Channel(models.TextChoices):
        MANUAL_AURIGA = "manual_auriga", "Manual (Auriga, Network Ops)"
        API = "api", "Supplier API"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Authorised, not yet executed"
        SENT = "SENT", "Executed / sent"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged by supplier"
        UNKNOWN = "UNKNOWN", "Outcome unknown"
        FAILED = "FAILED", "Failed (supplier rejected)"
        CANCELLED = "CANCELLED", "Cancelled before execution"
        DEAD_LETTER = "DEAD_LETTER", "Dead letter"

    OPEN_STATUSES = (Status.PENDING, Status.SENT, Status.UNKNOWN)

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    tenant = tenant_field()
    case = models.ForeignKey(SimTypeChangeCase, on_delete=models.PROTECT, related_name="commands")
    operation = models.CharField(max_length=16, choices=Operation.choices, default=Operation.SIM_CARD_SWAP)
    channel = models.CharField(max_length=16, choices=Channel.choices)
    idempotency_key = models.CharField(max_length=128, unique=True)
    correlation_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    # Hash of the supplier's response; the raw body is not kept here.
    response_digest = models.CharField(max_length=64, blank=True, default="")
    last_error = models.JSONField(null=True, blank=True)

    authorised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    authorised_at = models.DateTimeField(null=True, blank=True)
    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    executed_at = models.DateTimeField(null=True, blank=True)
    ticket_reference = models.CharField(max_length=64, blank=True, default="")
    target_serial_masked = models.CharField(max_length=32, blank=True, default="")
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One live supplier action per case.
            models.UniqueConstraint(
                fields=["case"],
                condition=Q(status__in=["PENDING", "SENT", "UNKNOWN"]),
                name="sim_change_one_open_command_per_case",
            ),
        ]

    def __str__(self):
        return f"{self.operation} · {self.channel} · {self.status}"


class SupplierVerificationEvidence(models.Model):
    """One supplier read used to judge whether the new SIM is bound (v2.1 §4.2).

    `binding_confirmed` is True only when every invariant matched: same
    subscriber and MSISDN, current serial is the reserved replacement ICCID,
    status Active, old serial no longer assigned, and plan/term unchanged.
    Identifiers are stored as fingerprints; the supplier response only as a hash.
    """

    tenant = tenant_field()
    case = models.ForeignKey(SimTypeChangeCase, on_delete=models.PROTECT, related_name="evidence")
    source = models.CharField(max_length=32)  # e.g. "subscriber_read", "auriga_manual"
    subscriber_id = models.CharField(max_length=64, blank=True, default="")
    msisdn_matches = models.BooleanField(default=False)
    subscriber_matches = models.BooleanField(default=False)
    current_serial_is_target = models.BooleanField(default=False)
    old_serial_detached = models.BooleanField(default=False)
    service_active = models.BooleanField(default=False)
    # Plan and term as we hold them; allowances, add-ons and billing cycle are
    # held by the supplier / usage platform and recorded in `checks` when read.
    commercial_terms_unchanged = models.BooleanField(default=False)
    checks = models.JSONField(default=dict, blank=True)
    supplier_status = models.CharField(max_length=32, blank=True, default="")
    target_serial_fingerprint = models.CharField(max_length=64, blank=True, default="")
    old_serial_fingerprint = models.CharField(max_length=64, blank=True, default="")
    response_sha256 = models.CharField(max_length=64, blank=True, default="")
    fetched_at = models.DateTimeField()
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fetched_at"]
        verbose_name_plural = "supplier verification evidence"

    def __str__(self):
        return f"evidence {self.source} @ {self.fetched_at:%Y-%m-%d %H:%M} · confirmed={self.binding_confirmed}"

    @property
    def binding_confirmed(self) -> bool:
        return all((
            self.msisdn_matches, self.subscriber_matches, self.current_serial_is_target,
            self.old_serial_detached, self.service_active, self.commercial_terms_unchanged,
        ))


class SwapSimStock(models.Model):
    """One supplier-recognised Swap SIM available for SIM type changes (v2.1 B4).

    A Swap SIM has no MSISDN; the swap binds the existing subscriber and
    number to it. Sales SIMs (pre-assigned numbers), SIMs of another tenant,
    SIMs already bound to a line and unverified records can never be reserved.

    The `Sim` row carries `inventory_purpose="swap"` so the shop never sells
    it. Reservation is confirmed by Network Ops against the supplier
    (`supplier_check_reference`) in Phase 1.
    """

    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        RESERVED = "RESERVED", "Reserved for a case"
        ASSIGNED = "ASSIGNED", "Bound to a line"
        QUARANTINED = "QUARANTINED", "Quarantined (needs Ops check)"
        RETIRED = "RETIRED", "Retired"

    tenant = tenant_field()
    sim = models.OneToOneField("sims.Sim", on_delete=models.PROTECT, related_name="swap_stock")
    sim_format = models.CharField(max_length=4, choices=SimType.choices)
    # Status as reported by the supplier / stock feed, e.g. "AVAILABLE".
    supplier_status = models.CharField(max_length=32, blank=True, default="")
    supplier_pool_id = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.AVAILABLE, db_index=True)

    reserved_case = models.ForeignKey(
        SimTypeChangeCase, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reservation_expires_at = models.DateTimeField(null=True, blank=True)
    reserved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Network Ops' confirmation that the supplier sees this SIM as an
    # unassigned Swap SIM in our pool (Auriga check / ticket reference).
    supplier_check_reference = models.CharField(max_length=128, blank=True, default="")
    quarantine_reason = models.CharField(max_length=255, blank=True, default="")

    # Provenance: where this stock record came from.
    source = models.CharField(max_length=128)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    imported_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["imported_at", "id"]
        verbose_name = "Swap SIM stock"
        verbose_name_plural = "Swap SIM stock"
        constraints = [
            # A case holds at most one reserved Swap SIM.
            models.UniqueConstraint(
                fields=["reserved_case"], condition=Q(status="RESERVED"),
                name="sim_change_one_reserved_swap_sim_per_case",
            ),
            models.CheckConstraint(
                condition=~Q(status="RESERVED") | Q(reserved_case__isnull=False),
                name="sim_change_reserved_stock_has_case",
            ),
        ]

    def __str__(self):
        return f"swap {self.sim_format} …{self.sim.iccid[-4:]} · {self.status}"


class SimChangePlanEligibility(models.Model):
    """Which plans may change SIM type, per direction (Addendum v2.1 B5).

    Versioned and disabled by default: a plan with no enabled, effective row
    is not eligible. Product enables plans once Commercial has approved them.
    """

    tenant = tenant_field()
    plan = models.ForeignKey("plans.Plan", on_delete=models.PROTECT, related_name="sim_change_eligibility")
    version = models.CharField(max_length=32)
    enabled = models.BooleanField(default=False)
    psim_to_esim = models.BooleanField(default=False)
    esim_to_psim = models.BooleanField(default=False)
    effective_from = models.DateTimeField()
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    approval_reference = models.CharField(max_length=128, blank=True, default="")
    notes = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["plan_id", "-effective_from"]
        verbose_name = "plan eligibility for SIM type change"
        verbose_name_plural = "plan eligibility for SIM type change"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "plan", "version"], name="sim_change_plan_eligibility_version"),
        ]

    def __str__(self):
        return f"{self.plan} v{self.version} ({'on' if self.enabled else 'off'})"

    def allows(self, direction: str) -> bool:
        if not self.enabled:
            return False
        return self.psim_to_esim if direction == Direction.PSIM_TO_ESIM else self.esim_to_psim


class IdempotencyRecord(models.Model):
    """Stored outcome of a request made with an Idempotency-Key (v2.0 §8.1).

    Scoped to tenant + user + operation (+ line). A replay with the same body
    gets the stored response; a replay with a different body is rejected.
    """

    tenant = tenant_field()
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    scope = models.CharField(max_length=96)
    key = models.CharField(max_length=128)
    request_digest = models.CharField(max_length=64)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "user", "scope", "key"], name="sim_change_idempotency_key"),
        ]

    def __str__(self):
        return f"{self.scope} · {self.key}"


class LineOperationLock(models.Model):
    """At most one line-changing operation per line at a time.

    Held while a SIM type change (and later a Task 04 replacement / suspension)
    is open; released when it ends. Acquire through `services.line_lock`.
    """

    class Operation(models.TextChoices):
        SIM_TYPE_CHANGE = "sim_type_change", "SIM type change"
        SIM_REPLACEMENT = "sim_replacement", "SIM replacement (Task 04)"
        SUSPENSION = "suspension", "Suspension (Task 04)"

    tenant = tenant_field()
    line = models.ForeignKey("add_line.Line", on_delete=models.PROTECT, related_name="operation_locks")
    operation = models.CharField(max_length=24, choices=Operation.choices)
    # Opaque reference to the holder, e.g. the case UUID.
    reference = models.CharField(max_length=64)
    acquired_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    release_reason = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["-acquired_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["line"], condition=Q(released_at__isnull=True), name="sim_change_one_lock_per_line",
            ),
        ]

    def __str__(self):
        state = "released" if self.released_at else "held"
        return f"{self.operation} lock on line {self.line_id} ({state})"
