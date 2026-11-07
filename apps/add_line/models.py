"""Data model for the "Add a New Number" feature.

Follows §07 of the Add New Number engineering decisions (v2.0):

    * A `Line` is one customer mobile line. It is owned by a Django user, never
      matched to one by email alone.
    * Payment, SIM activation, PAC acceptance and port completion are separate
      events, so `AddLineOrder` (money + fulfilment) and `PortRequest` (number
      transfer) are separate models with their own states.
    * `CapacityHold` is a slot against the per-account line cap; `LineEvent` is
      the immutable audit trail; `SupplierEvent` dedupes Transatel webhooks.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Term(models.TextChoices):
    DAYS_30 = "30D", "30 days"
    MONTHS_12 = "12M", "12 months"
    MONTHS_24 = "24M", "24 months"


class SimType(models.TextChoices):
    ESIM = "esim", "eSIM"
    PSIM = "psim", "Physical SIM"


class Line(models.Model):
    """One mobile line on a customer account.

    `current_msisdn` stays blank until Transatel allocates a number (this can
    happen at order, pre-activation or activation depending on the contract).
    After a completed PAC port it holds the ported number and
    `original_msisdn` keeps the temporary one.
    """

    class Status(models.TextChoices):
        PENDING_ACTIVATION = "pending_activation", "Pending activation"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        TERMINATED = "terminated", "Terminated"
        CANCELLED = "cancelled", "Cancelled"

    class Source(models.TextChoices):
        ADD_LINE = "add_line", "Added from account"
        LEGACY_CLAIM = "legacy_claim", "Claimed existing SIM"

    # Statuses that still occupy a slot against MAX_CONSUMER_LINES.
    CAPACITY_STATUSES = (Status.PENDING_ACTIVATION, Status.ACTIVE, Status.SUSPENDED)

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="lines"
    )
    sim = models.ForeignKey(
        "sims.Sim", on_delete=models.PROTECT, null=True, blank=True, related_name="lines"
    )
    plan = models.ForeignKey(
        "plans.Plan", on_delete=models.PROTECT, null=True, blank=True, related_name="lines"
    )
    term = models.CharField(max_length=3, choices=Term.choices, blank=True, default="")

    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.PENDING_ACTIVATION, db_index=True
    )
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.ADD_LINE)

    current_msisdn = models.CharField(max_length=24, blank=True, default="", db_index=True)
    original_msisdn = models.CharField(max_length=24, blank=True, default="")
    supplier_subscriber_id = models.CharField(max_length=64, blank=True, default="")

    activated_at = models.DateTimeField(null=True, blank=True)
    terminated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # A SIM can back at most one live line; terminated/cancelled lines
            # keep their history without blocking re-use.
            models.UniqueConstraint(
                fields=["sim"],
                condition=Q(status__in=["pending_activation", "active", "suspended"]),
                name="add_line_one_live_line_per_sim",
            ),
        ]

    def __str__(self):
        return f"{self.current_msisdn or 'number pending'} · {self.owner} · {self.get_status_display()}"

    @property
    def counts_toward_cap(self) -> bool:
        return self.status in self.CAPACITY_STATUSES


class CapacityHold(models.Model):
    """A reserved slot against the per-account line cap (decision A2).

    Created at checkout with `expires_at` = now + LINE_HOLD_MINUTES. Once
    payment succeeds `expires_at` is cleared so the slot is held until
    fulfilment creates the `Line`, at which point the hold is CONSUMED (the
    line itself then counts). Failed payment, cancellation or expiry RELEASE it.
    """

    class Status(models.TextChoices):
        HELD = "held", "Held"
        CONSUMED = "consumed", "Consumed by a line"
        RELEASED = "released", "Released"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="line_holds"
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.HELD, db_index=True
    )
    # Null while held means "paid, held until fulfilment" — never auto-expires.
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    release_reason = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"hold {self.uuid} · {self.owner} · {self.get_status_display()}"


class LineCapOverride(models.Model):
    """Support-granted exception to MAX_CONSUMER_LINES for one account (A2)."""

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="line_cap_override"
    )
    max_lines = models.PositiveSmallIntegerField()
    reason = models.CharField(max_length=255)
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.owner} · max {self.max_lines} lines"


class AddLineOrder(models.Model):
    """The purchase of one additional line: quote, payment and fulfilment.

    The number transfer (if any) lives on `PortRequest`, not here — a failed
    PAC does not make the order or the new line fail.
    """

    class SwitchChoice(models.TextChoices):
        PAC = "pac", "Keep my number (PAC)"
        STAC = "stac", "New number and end my old service (STAC)"
        NONE = "none", "New number, keep or no previous service"

    class PaymentStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"

    class FulfilmentStatus(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        SIM_RESERVED = "sim_reserved", "SIM reserved"
        ACTIVATING = "activating", "Activating"
        ACTIVATED = "activated", "Activated"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    order_reference = models.CharField(max_length=24, unique=True, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="add_line_orders"
    )
    hold = models.OneToOneField(
        CapacityHold, on_delete=models.PROTECT, null=True, blank=True, related_name="order"
    )
    line = models.OneToOneField(
        Line, on_delete=models.PROTECT, null=True, blank=True, related_name="order"
    )
    reserved_sim = models.ForeignKey(
        "sims.Sim", on_delete=models.PROTECT, null=True, blank=True,
        related_name="add_line_orders",
    )
    # Mirror row in sims.Order (same order_reference) so the existing eSIM QR /
    # pSIM details emails and their scheduled retry commands find the customer.
    sim_order = models.OneToOneField(
        "sims.Order", on_delete=models.PROTECT, null=True, blank=True,
        related_name="add_line_order",
    )

    # ── Quote snapshot (immutable once accepted) ──
    plan = models.ForeignKey("plans.Plan", on_delete=models.PROTECT, related_name="add_line_orders")
    term = models.CharField(max_length=3, choices=Term.choices)
    sim_type = models.CharField(max_length=4, choices=SimType.choices)
    switch_choice = models.CharField(max_length=4, choices=SwitchChoice.choices)
    quote = models.JSONField(default=dict)
    quote_hash = models.CharField(max_length=64, blank=True, default="")
    coupon = models.ForeignKey(
        "coupons.Coupon", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="add_line_orders",
    )
    currency = models.CharField(max_length=3, default="GBP")
    total_gross = models.DecimalField(max_digits=10, decimal_places=2)
    vat_amount = models.DecimalField(max_digits=10, decimal_places=2)

    # Billing contact sent to Transatel /contact-info (build_subscriber_info shape).
    billing = models.JSONField(default=dict, blank=True)
    # Physical SIM only; minimised to what fulfilment needs.
    delivery_address = models.JSONField(null=True, blank=True)

    # ── Payment ──
    payment_status = models.CharField(
        max_length=12, choices=PaymentStatus.choices, default=PaymentStatus.PENDING, db_index=True
    )
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    # Latest declined attempt (Stripe code + customer-safe message, no card data).
    # The order stays open so the customer can retry.
    last_payment_error = models.JSONField(null=True, blank=True)
    payment_failed_at = models.DateTimeField(null=True, blank=True)

    # ── Fulfilment ──
    fulfilment_status = models.CharField(
        max_length=16, choices=FulfilmentStatus.choices,
        default=FulfilmentStatus.NOT_STARTED, db_index=True,
    )
    errors = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.order_reference:
            self.order_reference = "AL-" + uuid.uuid4().hex[:10].upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.order_reference} · {self.owner} · {self.get_payment_status_display()}"


class ContractAcceptance(models.Model):
    """Evidence of what the customer agreed to at checkout (decision C12)."""

    order = models.OneToOneField(
        AddLineOrder, on_delete=models.PROTECT, related_name="contract_acceptance"
    )
    contract_version = models.CharField(max_length=32)
    contract_summary_version = models.CharField(max_length=32)
    documents_hash = models.CharField(max_length=64, blank=True, default="")
    accepted_at = models.DateTimeField()
    # Set only when the customer expressly asked for service to start inside
    # the 14-day cancellation period (enables pro-rata charging on cancel).
    immediate_service_requested_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default="")

    def __str__(self):
        return f"{self.order.order_reference} · contract {self.contract_version}"


class PortRequest(models.Model):
    """A PAC/STAC switch into Zoiko for one line (decisions B7, B8, C13).

    Created with AWAITING_ACTIVATION when the customer gives a code; only sent
    to Transatel once the line is active. A 201 from Transatel is PENDING, not
    accepted — the final result arrives later as VALIDATED / INVALIDATED /
    LOCKED / DONE events.
    """

    class CodeType(models.TextChoices):
        PAC = "PAC", "PAC (keep number)"
        STAC = "STAC", "STAC (new number, end old service)"

    class Status(models.TextChoices):
        AWAITING_ACTIVATION = "AWAITING_ACTIVATION", "Waiting for line activation"
        PENDING = "PENDING", "Pending"
        ATTACH_REQUESTED = "ATTACH_REQUESTED", "Attach requested"
        ATTACH_RECEIVED = "ATTACH_RECEIVED", "Attach received"
        ATTACH_FAILED = "ATTACH_FAILED", "Attach failed"
        VALIDATED = "VALIDATED", "Validated"
        INVALIDATED = "INVALIDATED", "Invalidated"
        LOCKED = "LOCKED", "Locked"
        DONE = "DONE", "Done"
        CANCELING = "CANCELING", "Cancelling"
        CANCELED = "CANCELED", "Cancelled"
        ERROR = "ERROR", "Error"

    # No further supplier transitions expected from these.
    TERMINAL_STATUSES = (
        Status.ATTACH_FAILED, Status.INVALIDATED, Status.DONE, Status.CANCELED, Status.ERROR,
    )

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    line = models.ForeignKey(Line, on_delete=models.PROTECT, related_name="port_requests")
    order = models.ForeignKey(
        AddLineOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="port_requests"
    )

    code_type = models.CharField(max_length=4, choices=CodeType.choices)
    # Fernet-encrypted PAC/STAC. Blanked by `purge_pac_codes` after the
    # transfer ends + PAC_RETENTION_DAYS; never shown in admin or the API.
    encrypted_code = models.TextField(blank=True, default="")
    code_purged_at = models.DateTimeField(null=True, blank=True)
    # The number being ported in (PAC only).
    portability_msisdn = models.CharField(max_length=24, blank=True, default="")
    wished_date = models.DateField(null=True, blank=True)
    code_expires_at = models.DateField(null=True, blank=True)

    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.AWAITING_ACTIVATION, db_index=True
    )
    supplier_transaction_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    supplier_request_id = models.CharField(max_length=64, blank=True, default="")
    # Raw supplier problem details — restricted, never sent to the customer.
    last_error = models.JSONField(null=True, blank=True)

    submitted_at = models.DateTimeField(null=True, blank=True)
    validated_at = models.DateTimeField(null=True, blank=True)
    port_date = models.DateField(null=True, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One in-flight transfer per line; a new one may follow a failure.
            models.UniqueConstraint(
                fields=["line"],
                condition=~Q(status__in=["ATTACH_FAILED", "INVALIDATED", "DONE", "CANCELED", "ERROR"]),
                name="add_line_one_open_port_per_line",
            ),
        ]

    def __str__(self):
        return f"{self.code_type} {self.portability_msisdn or ''} · {self.status}"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES


class SupplierEvent(models.Model):
    """One Transatel webhook delivery, stored before processing (decision B9).

    `event_id` is the supplier's header.eventId and is unique, so a retried
    delivery is recognised and processed only once.
    """

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSED = "processed", "Processed"
        IGNORED = "ignored", "Ignored"
        FAILED = "failed", "Failed"

    event_id = models.CharField(max_length=128, unique=True)
    event_type = models.CharField(max_length=128, db_index=True)
    event_date = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField()
    payload_sha256 = models.CharField(max_length=64)
    port_request = models.ForeignKey(
        PortRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.RECEIVED, db_index=True
    )
    error = models.TextField(blank=True, default="")
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.event_type} · {self.event_id}"


class LineClaim(models.Model):
    """A customer's request to attach an existing SIM to their account (A1).

    Never auto-approved on email alone: support (or a later high-confidence
    matcher) reviews the evidence and records who decided and why.
    """

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="line_claims"
    )
    iccid = models.CharField("ICCID", max_length=32, blank=True, default="")
    msisdn = models.CharField("MSISDN", max_length=24, blank=True, default="")
    order_reference = models.CharField(max_length=64, blank=True, default="")
    evidence = models.JSONField(default=dict, blank=True)

    matched_sim = models.ForeignKey(
        "sims.Sim", on_delete=models.SET_NULL, null=True, blank=True, related_name="claims"
    )
    matched_order = models.ForeignKey(
        "sims.Order", on_delete=models.SET_NULL, null=True, blank=True, related_name="claims"
    )
    resulting_line = models.OneToOneField(
        Line, on_delete=models.SET_NULL, null=True, blank=True, related_name="claim"
    )

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.SUBMITTED, db_index=True
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_line_claims",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"claim {self.iccid or self.msisdn or self.order_reference} · {self.owner} · {self.status}"


class LineEvent(models.Model):
    """Append-only audit trail of state changes across lines, orders and ports."""

    line = models.ForeignKey(
        Line, on_delete=models.CASCADE, null=True, blank=True, related_name="events"
    )
    order = models.ForeignKey(
        AddLineOrder, on_delete=models.CASCADE, null=True, blank=True, related_name="events"
    )
    port_request = models.ForeignKey(
        PortRequest, on_delete=models.CASCADE, null=True, blank=True, related_name="audit_events"
    )
    # Null when the change came from the system (webhook, job).
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    kind = models.CharField(max_length=64)
    status_before = models.CharField(max_length=32, blank=True, default="")
    status_after = models.CharField(max_length=32, blank=True, default="")
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind} {self.status_before}→{self.status_after}"
