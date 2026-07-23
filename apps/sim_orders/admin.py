from django.contrib import admin

from .models import SimProduct, SimOrder, SimActivationCode


@admin.register(SimProduct)
class SimProductAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "plan_name", "price_display", "is_active"]
    list_editable = ["is_active"]
    prepopulated_fields = {"slug": ("name",)}


@admin.register(SimOrder)
class SimOrderAdmin(admin.ModelAdmin):
    list_display = ["order_ref", "created_at", "customer_name", "email", "amount_display", "status"]
    list_filter = ["status", "created_at"]
    search_fields = ["order_ref", "email", "customer_name"]
    readonly_fields = ["order_ref", "stripe_session_id", "stripe_payment_intent_id", "created_at", "updated_at"]


@admin.register(SimActivationCode)
class SimActivationCodeAdmin(admin.ModelAdmin):
    list_display = ["code", "email", "order", "is_used", "expires_at", "created_at"]
    list_filter = ["is_used", "created_at"]
    search_fields = ["code", "email", "sim_serial", "order__order_ref"]
    readonly_fields = ["created_at"]


from .models import SimCartOrder  # noqa: E402


@admin.register(SimCartOrder)
class SimCartOrderAdmin(admin.ModelAdmin):
    list_display = ["order_ref", "created_at", "customer_name", "email", "order_type", "status"]
    list_filter = ["status", "order_type", "created_at"]
    search_fields = ["order_ref", "email", "customer_name"]
    readonly_fields = ["order_ref", "created_at"]
