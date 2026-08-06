from django.contrib import admin

from .models import RechargeModule, RechargeOrder


@admin.register(RechargeModule)
class RechargeModuleAdmin(admin.ModelAdmin):
    list_display = ["name", "status_display", "category", "shortcode", "enabled"]
    list_editable = ["enabled"]
    ordering = ["sort_order"]

    @admin.display(description="Status")
    def status_display(self, obj):
        return "Enabled" if obj.enabled else "Disabled"


@admin.register(RechargeOrder)
class RechargeOrderAdmin(admin.ModelAdmin):
    list_display = ["order_ref", "created_at", "customer_name", "msisdn", "amount_display", "status", "module"]
    list_filter = ["status", "module", "created_at"]
    search_fields = ["order_ref", "msisdn", "customer_name", "customer_email"]
    readonly_fields = ["order_ref", "stripe_session_id", "stripe_payment_intent_id", "created_at", "updated_at"]
