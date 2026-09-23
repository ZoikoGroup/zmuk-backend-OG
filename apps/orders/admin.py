from django.contrib import admin
from .models import BqOrder
from django.utils.html import format_html
import json


@admin.register(BqOrder)
class BqOrderAdmin(admin.ModelAdmin):
    list_display = ("id", "get_email", "get_bequick_order_id", "get_total", "created_at")
    ordering = ("-created_at",)
    readonly_fields = ("formatted_json",)

    fieldsets = (
        ("Bq Order Payload", {
            "classes": ("collapse",),
            "fields": ("formatted_json",),
        }),
    )

    def get_email(self, obj):
        return obj.raw_data.get("billingAddress", {}).get("email", "-")
    get_email.short_description = "Email"

    def get_bequick_order_id(self, obj):
        return obj.raw_data.get("bequick_order_id", "-")
    get_bequick_order_id.short_description = "BeQuick Order ID"

    def get_total(self, obj):
        return obj.raw_data.get("totals", {}).get("total", "-")
    get_total.short_description = "Total"

    def formatted_json(self, obj):
        pretty = json.dumps(obj.raw_data, indent=4, sort_keys=True)
        return format_html(
            """
            <div style="
                background:#0f172a;
                color:#e5e7eb;
                padding:15px;
                border-radius:10px;
                font-family: monospace;
                font-size:13px;
                max-height:600px;
                overflow:auto;
                white-space:pre;
            ">{}</div>
            """,
            pretty
        )

    formatted_json.short_description = "Bq Order JSON Data"
