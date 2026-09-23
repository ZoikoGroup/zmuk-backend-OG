from django.contrib import admin
from .models import MarineDiscountEnrollment


@admin.register(MarineDiscountEnrollment)
class MarineDiscountEnrollmentAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "ngo_name",
        "email",
        "dob",
        "id_type",
        "created_at",
    )
    search_fields = ("full_name", "email", "ngo_name")
    list_filter = ("created_at",)
