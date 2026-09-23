"""
Admin views for the Malima app.

Lets ops:
  • Bulk-load SIM inventory (via CSV import — see fixtures section in README)
  • Browse and search reservations / orders
  • Manually release stuck reservations
  • Mark SIMs as test / retired / suspended
"""

from django.contrib import admin, messages
from django.utils import timezone

from .models import MalimaOrder, MalimaOrderLine, SimInventory, SimReservation


# ── SimInventory ─────────────────────────────────────────────────────────────

@admin.register(SimInventory)
class SimInventoryAdmin(admin.ModelAdmin):
    list_display = ("id", "msisdn", "imsi", "iccid", "sim_type",
                    "roaming_zone", "status", "batch", "updated_at")
    list_filter = ("status", "sim_type", "roaming_zone", "batch")
    search_fields = ("msisdn", "imsi", "iccid", "batch")
    list_per_page = 50
    readonly_fields = ("created_at", "updated_at")
    actions = ("mark_available", "mark_test", "mark_retired")

    @admin.action(description="Mark selected SIMs as AVAILABLE")
    def mark_available(self, request, queryset):
        n = queryset.update(status=SimInventory.Status.AVAILABLE, updated_at=timezone.now())
        self.message_user(request, f"Marked {n} SIM(s) available.", messages.SUCCESS)

    @admin.action(description="Mark selected SIMs as TEST (ACTIVATED_FOR_TEST)")
    def mark_test(self, request, queryset):
        n = queryset.update(status=SimInventory.Status.TEST, updated_at=timezone.now())
        self.message_user(request, f"Marked {n} SIM(s) as test.", messages.SUCCESS)

    @admin.action(description="Mark selected SIMs as RETIRED")
    def mark_retired(self, request, queryset):
        n = queryset.update(status=SimInventory.Status.RETIRED, updated_at=timezone.now())
        self.message_user(request, f"Retired {n} SIM(s).", messages.SUCCESS)


# ── SimReservation ───────────────────────────────────────────────────────────

@admin.register(SimReservation)
class SimReservationAdmin(admin.ModelAdmin):
    list_display = ("reservation_id", "sim", "cart_index", "unit_index",
                    "status", "expires_at", "created_at")
    list_filter = ("status",)
    search_fields = ("reservation_id", "sim__msisdn", "sim__iccid", "plan_id")
    readonly_fields = ("created_at", "consumed_at", "released_at")
    autocomplete_fields = ("sim",)


# ── MalimaOrder ──────────────────────────────────────────────────────────────

class MalimaOrderLineInline(admin.TabularInline):
    model = MalimaOrderLine
    extra = 0
    can_delete = False
    readonly_fields = ("cart_index", "unit_index", "plan_id", "sim_type",
                       "msisdn", "iccid", "orange_order_id", "http_status",
                       "ok", "error", "created_at")
    fields = readonly_fields
    show_change_link = True


@admin.register(MalimaOrder)
class MalimaOrderAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "email", "total", "currency",
                    "stripe_payment_intent_id", "captured_at")
    list_filter = ("status", "currency")
    search_fields = ("email", "customer_name", "stripe_payment_intent_id",
                     "bequick_order_id", "lines__msisdn", "lines__iccid",
                     "lines__orange_order_id")
    readonly_fields = ("captured_at", "created_at", "updated_at", "raw_payload")
    date_hierarchy = "captured_at"
    inlines = (MalimaOrderLineInline,)


@admin.register(MalimaOrderLine)
class MalimaOrderLineAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "cart_index", "unit_index", "plan_id",
                    "msisdn", "orange_order_id", "ok", "http_status")
    list_filter = ("ok", "sim_type")
    search_fields = ("msisdn", "iccid", "orange_order_id", "plan_id")
    readonly_fields = ("request_payload", "response_payload", "created_at")
    autocomplete_fields = ("order", "reservation")
