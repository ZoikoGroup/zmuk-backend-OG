from django.contrib import admin
from .models import Coupon


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    # ❌ DO NOT show these anywhere
    exclude = (
        "used_count",
        "created_at",
        "updated_at",
    )

    list_display = (
        "name",
        "type",
        "discount",
        "limit",
        "is_use_once_per_customer",
    )

    search_fields = ("name", "slug")
    list_filter = ("type", "is_use_once_per_customer")

    filter_horizontal = ("users", "plans")

    prepopulated_fields = {"slug": ("name",)}
