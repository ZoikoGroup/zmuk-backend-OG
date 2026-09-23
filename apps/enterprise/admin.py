from django.contrib import admin
from .models import EnterpriseInquiry


@admin.register(EnterpriseInquiry)
class EnterpriseInquiryAdmin(admin.ModelAdmin):

    list_display = (
        "first_name",
        "last_name",
        "business_email",
        "company_name",
        "country",
        "inquiry_type",
        "email_sent",
        "created_at",
    )

    search_fields = (
        "first_name",
        "last_name",
        "business_email",
        "company_name",
    )

    list_filter = (
        "country",
        "inquiry_type",
        "created_at",
    )

    ordering = ("-created_at",)