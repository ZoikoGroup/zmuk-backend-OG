from django.contrib import admin

from . import services
from .models import SwitchFeedback, SwitchRequest, SwitchStatusEvent


class SwitchStatusEventInline(admin.TabularInline):
    model = SwitchStatusEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "kind", "status_before", "status_after", "actor", "note")
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


@admin.register(SwitchRequest)
class SwitchRequestAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "first_name",
        "last_name",
        "mobile",
        "selected_plan",
        "switch_route",
        "code_state",
        "current_provider",
        "status",
        "created_at",
    )
    list_filter = ("status", "switch_route", "contract_term", "sim_type", "current_provider", "created_at")
    search_fields = ("reference", "first_name", "last_name", "email", "mobile", "postcode")
    list_editable = ("status",)
    # The encrypted code is never shown; `code_state` says whether one is held.
    readonly_fields = (
        "reference", "user", "code_type", "code_state", "code_received_at", "code_purged_at",
        "completed_at", "created_at", "updated_at",
    )
    raw_id_fields = ("plan", "order", "line")
    ordering = ("-created_at",)
    inlines = [SwitchStatusEventInline, SwitchFeedbackInline]

    fieldsets = (
        ("Request", {"fields": ("reference", "user")}),
        ("Customer", {"fields": ("first_name", "last_name", "email", "mobile", "postcode")}),
        (
            "Current Service",
            {"fields": ("current_provider", "current_plan_cost", "current_data_allowance")},
        ),
        ("New Plan", {"fields": ("plan", "selected_plan", "contract_term", "sim_type", "promo_code")}),
        (
            "Number Transfer",
            {"fields": ("switch_route", "port_number", "pac_later", "code_type", "code_state",
                        "code_received_at", "code_purged_at")},
        ),
        (
            "Consent",
            {"fields": ("transfer_authorised", "timeline_acknowledged", "terms_accepted",
                        "opt_in_news", "opt_in_texts")},
        ),
        (
            "Processing",
            {"fields": ("status", "estimated_completion_date", "order", "line", "completed_at")},
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="Code")
    def code_state(self, obj):
        return services.pac_status(obj).replace("_", " ")

    def save_model(self, request, obj, form, change):
        obj._status_actor = request.user  # recorded on the status history
        super().save_model(request, obj, form, change)
