from django.contrib import admin

from .models import TravelEcosystemPartnerInquiry


@admin.register(TravelEcosystemPartnerInquiry)
class TravelEcosystemPartnerInquiryAdmin(admin.ModelAdmin):

    list_display = (
        "first_name",
        "last_name",
        "company_name",
        "work_email",
        "country_region",
        "monthly_traveler_volume",
        "created_at",
    )

    search_fields = (
        "first_name",
        "last_name",
        "company_name",
        "work_email",
    )

    list_filter = (
        "country_region",
        "monthly_traveler_volume",
        "created_at",
    )

    ordering = ("-created_at",)