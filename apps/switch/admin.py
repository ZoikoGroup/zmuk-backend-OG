from django.contrib import admin

from .models import SwitchRequest


@admin.register(SwitchRequest)
class SwitchRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "first_name",
        "last_name",
        "mobile",
        "selected_plan",
        "current_provider",
        "status",
        "created_at",
    )
    list_filter = ("status", "selected_plan", "current_provider", "created_at")
    search_fields = ("first_name", "last_name", "email", "mobile", "postcode")
    list_editable = ("status",)
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-created_at",)

    fieldsets = (
        ("Customer", {"fields": ("first_name", "last_name", "email", "mobile", "postcode")}),
        (
            "Current Service",
            {"fields": ("current_provider", "current_plan_cost", "current_data_allowance")},
        ),
        ("New Plan", {"fields": ("selected_plan",)}),
        (
            "Authorisation",
            {"fields": ("transfer_authorised", "timeline_acknowledged", "terms_accepted")},
        ),
        ("Switch Processing", {"fields": ("status", "pac_code", "stac_code")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )
