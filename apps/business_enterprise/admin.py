from django.contrib import admin
from .models import EnterpriseInquiry


@admin.register(EnterpriseInquiry)
class EnterpriseInquiryAdmin(admin.ModelAdmin):

    list_display = (
        "full_name",
        "company_name",
        "business_email",
        "role",
        "enquiry_type",
        "created_at",
    )

    search_fields = (
        "full_name",
        "company_name",
        "business_email",
    )

    list_filter = (
        "role",
        "enquiry_type",
        "created_at",
    )

    ordering = ("-created_at",)