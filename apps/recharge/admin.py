from django.contrib import admin

from .models import RechargeModule, RechargeOrder, RechargeProduct, ReactivationAttempt, TransatelLog


@admin.register(RechargeModule)
class RechargeModuleAdmin(admin.ModelAdmin):
    list_display = ["name", "status_display", "category", "shortcode", "enabled"]
    list_editable = ["enabled"]
    ordering = ["sort_order"]

    @admin.display(description="Status")
    def status_display(self, obj):
        return "Enabled" if obj.enabled else "Disabled"


@admin.register(RechargeProduct)
class RechargeProductAdmin(admin.ModelAdmin):
    list_display = ["name", "price_display", "module", "rate_plan", "is_active", "sort_order"]
    list_filter = ["module", "is_active"]
    list_editable = ["is_active", "sort_order"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}


@admin.register(RechargeOrder)
class RechargeOrderAdmin(admin.ModelAdmin):
    list_display = [
        "order_ref", "created_at", "customer_name", "msisdn",
        "amount_display", "status", "module", "reactivation_status_display",
    ]
    list_filter = ["status", "module", "created_at"]
    search_fields = ["order_ref", "msisdn", "customer_name", "customer_email", "sim_serial"]
    readonly_fields = [
        "order_ref", "stripe_session_id", "stripe_payment_intent_id",
        "created_at", "updated_at", "paid_at", "completed_at",
    ]

    @admin.display(description="Reactivation")
    def reactivation_status_display(self, obj):
        attempt = obj.reactivation_attempts.order_by("-updated_at").first()
        if not attempt:
            return "—"
        return attempt.status.upper()


@admin.register(ReactivationAttempt)
class ReactivationAttemptAdmin(admin.ModelAdmin):
    list_display = [
        "order", "sim_serial_short", "status", "attempts",
        "provider_transaction_id", "created_at",
    ]
    list_filter = ["status"]
    readonly_fields = ["idempotency_key", "created_at", "updated_at"]

    @admin.display(description="SIM")
    def sim_serial_short(self, obj):
        return f"***{obj.sim_serial[-4:]}" if obj.sim_serial else "—"


@admin.register(TransatelLog)
class TransatelLogAdmin(admin.ModelAdmin):
    list_display = [
        "created_at", "action", "success", "response_status",
        "duration_ms", "sim_serial_masked", "order_ref_display",
    ]
    list_filter = ["action", "success"]
    readonly_fields = [
        "order", "action", "sim_serial_masked", "msisdn_masked",
        "request_url", "request_method", "request_body",
        "response_status", "response_body", "error_message",
        "success", "duration_ms", "created_at",
    ]

    @admin.display(description="Order")
    def order_ref_display(self, obj):
        return obj.order.order_ref if obj.order else "—"
