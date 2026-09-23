from django.contrib import admin
from .models import TravelPartner


@admin.register(TravelPartner)
class TravelPartnerAdmin(admin.ModelAdmin):

    list_display = (
        "id",
        "full_name",
        "company_name",
        "business_email",
        "role",
        "consent",
        "email_sent",
        "created_at",
    )

    search_fields = (
        "full_name",
        "company_name",
        "business_email",
    )

    list_filter = (
        "role",
        "consent",
        "created_at",
    )

    ordering = ("-created_at",)