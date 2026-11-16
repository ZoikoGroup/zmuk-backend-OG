from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Q

from django.utils import timezone

from . import continuation, documents, receipts, services
from .models import (
    ContractTermRule,
    ContractTerms,
    PlanTariff,
    SwitchContinuationToken,
    SwitchContractConsent,
    SwitchContractDocument,
    SwitchContractPack,
    SwitchDocumentDelivery,
    SwitchFeedback,
    SwitchPaymentAdjustment,
    SwitchPaymentCapture,
    SwitchPaymentReceipt,
    SwitchRequest,
    SwitchStatusEvent,
)

S = SwitchRequest.Status


class SwitchStatusEventInline(admin.TabularInline):
    model = SwitchStatusEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "kind", "status_before", "status_after", "action_reason", "action_owner",
              "actor", "note")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class SwitchContinuationTokenInline(admin.TabularInline):
    """Links sent for this request. The token itself is never stored."""

    model = SwitchContinuationToken
    fk_name = "switch_request"
    extra = 0
    can_delete = False
    fields = ("created_at", "sent_to", "delivered", "expires_at", "used_at", "used_by", "revoked_at", "created_by")
    readonly_fields = fields
    verbose_name = "continuation link"

    def has_add_permission(self, request, obj=None):
        return False


class SwitchFeedbackInline(admin.StackedInline):
    model = SwitchFeedback
    extra = 0
    can_delete = False
    readonly_fields = ("rating", "comments", "created_at")

    def has_add_permission(self, request, obj=None):
        return False


class OpsQueueFilter(admin.SimpleListFilter):
    """Who the request is waiting on (engineering note 13 Nov 2026)."""

    title = "queue"
    parameter_name = "queue"

    def lookups(self, request, model_admin):
        return [
            ("operations", "Waiting on Operations (checking / ops action)"),
            ("customer", "Waiting on the customer"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "operations":
            return queryset.filter(
                Q(status__in=[S.RECEIVED, S.VALIDATING])
                | Q(status=S.ACTION_NEEDED, action_owner=SwitchRequest.ActionOwner.OPERATIONS)
            )
        if self.value() == "customer":
            return queryset.filter(status=S.ACTION_NEEDED, action_owner=SwitchRequest.ActionOwner.CUSTOMER)
        return queryset


@admin.register(SwitchRequest)
class SwitchRequestAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "first_name",
        "mobile_masked",
        "selected_plan",
        "switch_route",
        "code_state",
        "status",
        "action_reason",
        "action_owner",
        "created_at",
    )
    list_filter = (OpsQueueFilter, "status", "action_reason", "action_owner", "switch_route", "contract_term",
                   "sim_type", "existing_account_email", "created_at")
    search_fields = ("reference", "first_name", "last_name", "email", "mobile", "postcode")
    # The encrypted code is never shown; `code_state` says whether one is held.
    readonly_fields = (
        "reference", "user", "existing_account_email", "idempotency_key",
        "code_type", "code_state", "code_received_at", "code_purged_at",
        "quote_snapshot", "quote_hash", "precontract_documents_sent_at",
        "consent_version", "consented_at", "consent_ip", "consent_user_agent",
        "converted_at", "claimed_at", "created_at", "updated_at",
    )
    raw_id_fields = ("plan", "order", "line")
    ordering = ("-created_at",)
    inlines = [SwitchStatusEventInline, SwitchContinuationTokenInline, SwitchFeedbackInline]
    actions = ["mark_validating", "mark_ready_for_order", "send_continuation_link", "mark_rejected",
               "mark_cancelled"]

    fieldsets = (
        ("Request", {"fields": ("reference", "user", "claimed_at", "existing_account_email", "idempotency_key")}),
        ("Customer", {"fields": ("first_name", "last_name", "email", "mobile")}),
        ("Billing / delivery address", {"fields": ("address_line_1", "address_line_2", "town_city", "postcode")}),
        (
            "Current Service",
            {"fields": ("current_provider", "current_plan_cost", "current_data_allowance")},
        ),
        (
            "New Plan",
            {"fields": ("plan", "selected_plan", "contract_term", "sim_type", "promo_code",
                        "quote_snapshot", "quote_hash", "precontract_documents_sent_at")},
        ),
        (
            "Number Transfer",
            {"fields": ("switch_route", "port_number", "pac_later", "code_type", "code_state",
                        "code_received_at", "code_purged_at")},
        ),
        (
            "Consent",
            {"fields": ("transfer_authorised", "timeline_acknowledged", "terms_accepted",
                        "consent_version", "consented_at", "consent_ip", "consent_user_agent",
                        "opt_in_news", "opt_in_texts")},
        ),
        (
            "Processing",
            {"fields": ("status", "action_reason", "action_owner", "ops_note", "confirmed_switch_date",
                        "order", "line", "converted_at")},
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        # Phase 1 can't link orders/lines (M04 §07, T20).
        if not settings.SWITCH_ORDER_CONVERSION_ENABLED:
            fields += ["order", "line"]
        return fields

    @admin.display(description="Mobile")
    def mobile_masked(self, obj):
        return services.mask_number(obj.mobile)

    @admin.display(description="Code")
    def code_state(self, obj):
        return services.pac_status(obj).replace("_", " ")

    def save_model(self, request, obj, form, change):
        obj._status_actor = request.user  # recorded on the status history
        super().save_model(request, obj, form, change)

    def _bulk_status(self, request, queryset, status, allowed_from):
        moved = []
        for switch in queryset:
            if switch.status in allowed_from:
                switch.set_status(status, actor=request.user)
                moved.append(switch)
        skipped = queryset.count() - len(moved)
        self.message_user(request, f"{len(moved)} request(s) updated.", messages.SUCCESS)
        if skipped:
            self.message_user(request, f"{skipped} skipped (status didn't allow it).", messages.WARNING)
        return moved

    def _send_links(self, request, switches):
        sent = failed = 0
        for switch in switches:
            try:
                token = continuation.issue_link(switch, actor=request.user)
            except continuation.ContinuationError:
                failed += 1
                continue
            sent += token.delivered
            failed += not token.delivered
        if sent:
            self.message_user(request, f"Continuation link emailed for {sent} request(s).", messages.SUCCESS)
        if failed:
            self.message_user(request, f"{failed} link(s) not emailed (not ready, or the email failed).",
                              messages.WARNING)

    @admin.action(description="Move to: checking (validating)")
    def mark_validating(self, request, queryset):
        self._bulk_status(request, queryset, S.VALIDATING, (S.RECEIVED, S.ACTION_NEEDED))

    @admin.action(description="Move to: ready for order (emails the continuation link if enabled)")
    def mark_ready_for_order(self, request, queryset):
        moved = self._bulk_status(request, queryset, S.READY_FOR_ORDER,
                                  (S.RECEIVED, S.VALIDATING, S.ACTION_NEEDED))
        if moved and continuation.enabled():
            self._send_links(request, moved)

    @admin.action(description="Send a new continuation link (ready-for-order requests)")
    def send_continuation_link(self, request, queryset):
        if not continuation.enabled():
            self.message_user(request, "Online order completion is switched off "
                                       "(SWITCH_ORDER_CONVERSION_ENABLED).", messages.WARNING)
            return
        self._send_links(request, queryset)

    @admin.action(description="Reject (unable to proceed)")
    def mark_rejected(self, request, queryset):
        self._bulk_status(request, queryset, S.REJECTED,
                          (S.RECEIVED, S.VALIDATING, S.ACTION_NEEDED, S.READY_FOR_ORDER))

    @admin.action(description="Cancel")
    def mark_cancelled(self, request, queryset):
        self._bulk_status(request, queryset, S.CANCELLED,
                          (S.RECEIVED, S.VALIDATING, S.ACTION_NEEDED, S.READY_FOR_ORDER))


# ── Milestone 4 · B2: contract content and issued documents ───────────────────

class _ApprovedIsFrozen:
    """Approved content can't be edited in place: create a new version, so the
    record of what a customer was given never changes (CS-10)."""

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.approved:
            return [f.name for f in self.model._meta.fields]
        return list(super().get_readonly_fields(request, obj))


class ContractTermRuleInline(admin.StackedInline):
    model = ContractTermRule
    extra = 0

    def has_change_permission(self, request, obj=None):
        return not (obj and obj.approved)

    def has_add_permission(self, request, obj=None):
        return not (obj and obj.approved)

    def has_delete_permission(self, request, obj=None):
        return not (obj and obj.approved)


@admin.register(ContractTerms)
class ContractTermsAdmin(_ApprovedIsFrozen, admin.ModelAdmin):
    list_display = ("version", "approved", "approved_at", "approved_by", "missing_count")
    readonly_fields = ("approved_by", "approved_at")
    inlines = [ContractTermRuleInline]

    @admin.display(description="Blank fields")
    def missing_count(self, obj):
        blank = [f for f in documents._TERMS_FIELDS if not str(getattr(obj, f) or "").strip()]
        return len(blank) or "—"

    def save_model(self, request, obj, form, change):
        if obj.approved and not obj.approved_at:
            obj.approved_at, obj.approved_by = timezone.now(), request.user
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        return not (obj and obj.approved)


@admin.register(PlanTariff)
class PlanTariffAdmin(_ApprovedIsFrozen, admin.ModelAdmin):
    list_display = ("plan", "version", "approved", "approved_at")
    list_filter = ("approved",)
    readonly_fields = ("approved_at",)
    raw_id_fields = ("plan",)

    def save_model(self, request, obj, form, change):
        if obj.approved and not obj.approved_at:
            obj.approved_at = timezone.now()
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        return not (obj and obj.approved)


class SwitchContractDocumentInline(admin.TabularInline):
    model = SwitchContractDocument
    extra = 0
    can_delete = False
    fields = ("kind", "filename", "size", "sha256", "template_version", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class SwitchContractConsentInline(admin.StackedInline):
    model = SwitchContractConsent
    extra = 0
    can_delete = False
    fields = ("consented_at", "customer", "statement", "statement_version", "documents",
              "immediate_service_requested", "ip_address", "user_agent", "order")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SwitchContractPack)
class SwitchContractPackAdmin(admin.ModelAdmin):
    """Issued documents and consent: evidence, so view-only."""

    list_display = ("reference", "switch_request", "customer", "created_at", "offer_valid_until",
                    "email_status", "superseded_at", "consented")
    list_filter = ("email_status",)
    search_fields = ("reference", "switch_request__reference")
    readonly_fields = [f.name for f in SwitchContractPack._meta.fields]
    inlines = [SwitchContractDocumentInline, SwitchContractConsentInline]

    @admin.display(boolean=True)
    def consented(self, obj):
        return hasattr(obj, "consent")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ── Milestone 4 · B4: payments and receipts (view-only evidence) ─────────────

class SwitchPaymentReceiptInline(admin.StackedInline):
    model = SwitchPaymentReceipt
    extra = 0
    can_delete = False
    fields = ("receipt_number", "filename", "size", "sha256", "template_version", "issued_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class SwitchPaymentAdjustmentInline(admin.TabularInline):
    model = SwitchPaymentAdjustment
    extra = 0
    can_delete = False
    fields = ("occurred_at", "kind", "amount_minor", "provider_status", "provider_event_id")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SwitchPaymentCapture)
class SwitchPaymentCaptureAdmin(admin.ModelAdmin):
    list_display = ("switch_request", "order", "amount", "method_label", "captured_at", "status",
                    "receipt_status", "receipt_attempts")
    list_filter = ("receipt_status", "status")
    search_fields = ("switch_request__reference", "order__order_reference", "receipt__receipt_number")
    readonly_fields = [f.name for f in SwitchPaymentCapture._meta.fields]
    inlines = [SwitchPaymentReceiptInline, SwitchPaymentAdjustmentInline]
    actions = ["resend_receipt", "retry_receipt"]

    @admin.display(description="Amount")
    def amount(self, obj):
        return f"£{obj.amount_minor / 100:.2f}"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Re-send the original receipt by email (logged)")
    def resend_receipt(self, request, queryset):
        sent = failed = 0
        for capture in queryset:
            receipt = getattr(capture, "receipt", None)
            if receipt is None:
                failed += 1
                continue
            if receipts.email_receipt(receipt, actor=request.user, resend=True):
                sent += 1
            else:
                failed += 1
        if sent:
            self.message_user(request, f"{sent} receipt(s) re-sent.", messages.SUCCESS)
        if failed:
            self.message_user(request, f"{failed} not sent (no receipt yet, or the email failed).",
                              messages.WARNING)

    @admin.action(description="Retry creating the receipt")
    def retry_receipt(self, request, queryset):
        issued = sum(1 for capture in queryset if receipts.issue_receipt(capture))
        self.message_user(request, f"{issued} receipt(s) ready.", messages.SUCCESS)


@admin.register(SwitchDocumentDelivery)
class SwitchDocumentDeliveryAdmin(admin.ModelAdmin):
    """Receipt emails, including staff re-sends. Filter "accepted: No" for failures."""

    list_display = ("created_at", "receipt", "recipient_masked", "accepted", "resend", "actor")
    list_filter = ("accepted", "resend")
    readonly_fields = [f.name for f in SwitchDocumentDelivery._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
