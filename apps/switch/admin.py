from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Q

from . import services
from .models import SwitchFeedback, SwitchRequest, SwitchStatusEvent

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
        "converted_at", "created_at", "updated_at",
    )
    raw_id_fields = ("plan", "order", "line")
    ordering = ("-created_at",)
    inlines = [SwitchStatusEventInline, SwitchFeedbackInline]
    actions = ["mark_validating", "mark_ready_for_order", "mark_rejected", "mark_cancelled"]

    fieldsets = (
        ("Request", {"fields": ("reference", "user", "existing_account_email", "idempotency_key")}),
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
        moved = 0
        for switch in queryset:
            if switch.status in allowed_from:
                switch.set_status(status, actor=request.user)
                moved += 1
        skipped = queryset.count() - moved
        self.message_user(request, f"{moved} request(s) updated.", messages.SUCCESS)
        if skipped:
            self.message_user(request, f"{skipped} skipped (status didn't allow it).", messages.WARNING)

    @admin.action(description="Move to: checking (validating)")
    def mark_validating(self, request, queryset):
        self._bulk_status(request, queryset, S.VALIDATING, (S.RECEIVED, S.ACTION_NEEDED))

    @admin.action(description="Move to: ready for order")
    def mark_ready_for_order(self, request, queryset):
        self._bulk_status(request, queryset, S.READY_FOR_ORDER, (S.RECEIVED, S.VALIDATING, S.ACTION_NEEDED))

    @admin.action(description="Reject (unable to proceed)")
    def mark_rejected(self, request, queryset):
        self._bulk_status(request, queryset, S.REJECTED,
                          (S.RECEIVED, S.VALIDATING, S.ACTION_NEEDED, S.READY_FOR_ORDER))

    @admin.action(description="Cancel")
    def mark_cancelled(self, request, queryset):
        self._bulk_status(request, queryset, S.CANCELLED,
                          (S.RECEIVED, S.VALIDATING, S.ACTION_NEEDED, S.READY_FOR_ORDER))
