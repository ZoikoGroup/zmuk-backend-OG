import hashlib

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import Q

from django.utils import timezone

from . import continuation, documents, receipts, services
from .models import (
    ContractTermRule,
    ContractTerms,
    PlanPriceChange,
    PlanTariff,
    SwitchContinuationToken,
    SwitchContractConsent,
    SwitchContractDocument,
    SwitchContractPack,
    SwitchContractProvision,
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
        "consent_version", "consent_statement", "consented_at", "consent_ip", "consent_user_agent",
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
                        "consent_version", "consent_statement", "consented_at", "consent_ip",
                        "consent_user_agent",
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
        # A request parked for missing contract content only reopens once the
        # approved terms and tariff for its plan are complete; otherwise the
        # customer would hit the same failure again.
        still_blocked = [
            s.pk for s in queryset.filter(status=S.ACTION_NEEDED,
                                          action_reason=SwitchRequest.ActionReason.CONTRACT_DOCS)
            if documents.missing_content(documents.find_sources(s.plan_id, s.contract_term))
        ]
        if still_blocked:
            self.message_user(request, f"{len(still_blocked)} request(s) kept on hold: their plan's approved "
                                       "contract terms / tariff are still incomplete.", messages.WARNING)
            queryset = queryset.exclude(pk__in=still_blocked)
        moved = self._bulk_status(request, queryset, S.READY_FOR_ORDER,
                                  (S.RECEIVED, S.VALIDATING, S.ACTION_NEEDED))
        if moved and continuation.enabled():
            self._send_links(request, moved)

    @admin.action(description="Send a new continuation link (ready-for-order requests)")
    def send_continuation_link(self, request, queryset):
        if not continuation.enabled():
            self.message_user(request, f"Online order completion is switched off "
                                       f"({continuation.disabled_reason()}).", messages.WARNING)
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

SOURCE_FILE_MAX_BYTES = 25 * 1024 * 1024


class ApprovedContentForm(forms.ModelForm):
    """Attaching the signed master fills in its file name and SHA-256. The file
    itself is not stored here: the controlled copy stays in the restricted
    repository named in `source_location` (v2.3 §1A)."""

    source_file = forms.FileField(
        required=False,
        help_text="Optional: choose the signed / approved master file to record its name and SHA-256 "
                  "(the file is not uploaded or kept).",
    )

    def clean(self):
        cleaned = super().clean()
        upload = cleaned.get("source_file")
        if upload:
            if upload.size > SOURCE_FILE_MAX_BYTES:
                raise forms.ValidationError({"source_file": "That file is larger than 25 MB."})
            digest = hashlib.sha256()
            for chunk in upload.chunks():
                digest.update(chunk)
            cleaned["source_document"] = upload.name[:255]
            cleaned["source_sha256"] = digest.hexdigest()
        if cleaned.get("source_sha256"):
            cleaned["source_sha256"] = cleaned["source_sha256"].strip().lower()
        return cleaned


class _ApprovalAdmin(admin.ModelAdmin):
    """Draft → approved → published → retired, by admin action only.

    Recording approval and publishing each need their own permission
    (`approve_<model>`, `publish_<model>`). From approval on, the record is
    read-only: a change is a new version, so the record of what a customer
    was given never changes (CS-10, v2.3 §5).
    """

    form = ApprovedContentForm
    LIFECYCLE_FIELDS = ("state", "approval_check", "approved_by", "approved_at", "published_by", "published_at",
                        "retired_at")
    EVIDENCE_FIELDSET = ("Approval evidence", {
        "fields": ("approval_reference", "approval_date", "document_owner", "source_file", "source_document",
                   "source_sha256", "source_location", "effective_from", "effective_to"),
        "description": "Record the existing approval. Never fill this in from a draft, a competitor or "
                       "generated text: missing approved content goes to Legal / Product.",
    })
    LIFECYCLE_FIELDSET = ("Status", {"fields": LIFECYCLE_FIELDS})
    actions = ["record_approval", "publish", "retire", "return_to_draft"]

    def _perm(self, request, kind):
        return request.user.has_perm(f"{self.opts.app_label}.{kind}_{self.opts.model_name}")

    def has_approve_permission(self, request):
        return self._perm(request, "approve")

    def has_publish_permission(self, request):
        return self._perm(request, "publish")

    def get_form(self, request, obj=None, **kwargs):
        if obj and obj.frozen:
            kwargs["form"] = forms.ModelForm  # read-only: no file field
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj and obj.frozen:
            return [(name, {**opts, "fields": [f for f in opts["fields"] if f != "source_file"]})
                    for name, opts in fieldsets]
        return fieldsets

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.frozen:
            return [f.name for f in self.model._meta.fields if f.name != "id"] + list(self.LIFECYCLE_FIELDS)
        return list(self.LIFECYCLE_FIELDS)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and obj.frozen)

    @admin.display(description="Approval check")
    def approval_check(self, obj):
        if not obj or not obj.pk:
            return "Save the draft first."
        if obj.state != obj.State.DRAFT:
            return "—"
        problems = obj.approval_problems()
        return "Ready to record approval." if not problems else "Still needed: " + "; ".join(problems)

    def _transition(self, request, queryset, method, done, *args):
        ok = 0
        for obj in queryset:
            try:
                getattr(obj, method)(*args)
                ok += 1
            except ValidationError as exc:
                self.message_user(request, f"{obj}: " + "; ".join(exc.messages), messages.ERROR)
        if ok:
            self.message_user(request, f"{ok} version(s) {done}.", messages.SUCCESS)

    @admin.action(description="Record approval (evidence required)", permissions=["approve"])
    def record_approval(self, request, queryset):
        self._transition(request, queryset, "record_approval", "approved", request.user)

    @admin.action(description="Publish (use for customers from 'effective from')", permissions=["publish"])
    def publish(self, request, queryset):
        self._transition(request, queryset, "publish", "published", request.user)

    @admin.action(description="Retire (stop using from today)", permissions=["publish"])
    def retire(self, request, queryset):
        self._transition(request, queryset, "retire", "retired")

    @admin.action(description="Return to draft (approved, never published)", permissions=["approve"])
    def return_to_draft(self, request, queryset):
        self._transition(request, queryset, "return_to_draft", "returned to draft")


class _DraftOnlyInline:
    """Child rows follow their parent: editable only while it is a draft."""

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and obj.frozen)

    def has_add_permission(self, request, obj=None):
        return super().has_add_permission(request, obj) and not (obj and obj.frozen)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and obj.frozen)


class ContractTermRuleInline(_DraftOnlyInline, admin.StackedInline):
    model = ContractTermRule
    extra = 0


@admin.register(ContractTerms)
class ContractTermsAdmin(_ApprovalAdmin):
    list_display = ("version", "state", "effective_from", "effective_to", "approval_reference", "problem_count")
    list_filter = ("state",)
    inlines = [ContractTermRuleInline]
    fieldsets = (
        ("Version", {"fields": ("version",)}),
        _ApprovalAdmin.LIFECYCLE_FIELDSET,
        _ApprovalAdmin.EVIDENCE_FIELDSET,
        ("Issuer", {"fields": ContractTerms.TEXT_FIELDS[:6]}),
        ("Approved wording", {"fields": ContractTerms.TEXT_FIELDS[6:]}),
    )

    @admin.display(description="Problems")
    def problem_count(self, obj):
        return len(obj.approval_problems()) if obj.state == obj.State.DRAFT else "—"


class PlanPriceChangeInline(_DraftOnlyInline, admin.TabularInline):
    model = PlanPriceChange
    extra = 0


@admin.register(PlanTariff)
class PlanTariffAdmin(_ApprovalAdmin):
    list_display = ("plan", "version", "state", "plan_sku", "tariff_code", "effective_from", "effective_to",
                    "problem_count")
    list_filter = ("state",)
    search_fields = ("plan__name", "plan_sku", "tariff_code")
    raw_id_fields = ("plan",)
    inlines = [PlanPriceChangeInline]
    fieldsets = (
        ("Plan", {"fields": ("plan", "version", "plan_sku", "tariff_code", "vat_basis")}),
        _ApprovalAdmin.LIFECYCLE_FIELDSET,
        _ApprovalAdmin.EVIDENCE_FIELDSET,
        ("Allowances", {"fields": ("uk_data", "uk_minutes", "uk_texts", "fair_use", "speeds")}),
        ("Out of allowance and rollover", {"fields": ("after_allowance", "extra_charges", "rollover_applies",
                                                      "rollover")}),
        ("Roaming", {"fields": ("roaming_zone", "roaming_allowance", "roaming_rates")}),
        ("Charges and price changes", {"fields": ("one_off_charges", "price_changes_apply",
                                                  "scheduled_price_changes")}),
    )

    @admin.display(description="Problems")
    def problem_count(self, obj):
        return len(obj.approval_problems()) if obj.state == obj.State.DRAFT else "—"


class SwitchContractDocumentInline(admin.TabularInline):
    model = SwitchContractDocument
    extra = 0
    can_delete = False
    fields = ("kind", "filename", "size", "sha256", "template_version", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class SwitchContractProvisionInline(admin.TabularInline):
    """How and when the documents were provided (evidence, view-only)."""

    model = SwitchContractProvision
    extra = 0
    can_delete = False
    fields = ("created_at", "method", "document", "succeeded", "recipient_masked", "actor", "ip_address")
    readonly_fields = fields
    verbose_name = "provision event"

    def has_add_permission(self, request, obj=None):
        return False


class SwitchContractConsentInline(admin.StackedInline):
    model = SwitchContractConsent
    extra = 0
    can_delete = False
    fields = ("consented_at", "customer", "statement", "statement_version", "documents", "provision",
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
    inlines = [SwitchContractDocumentInline, SwitchContractProvisionInline, SwitchContractConsentInline]

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
