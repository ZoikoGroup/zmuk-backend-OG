from django.contrib import admin
from .models import MilitaryDiscountEnrollment


@admin.register(MilitaryDiscountEnrollment)
class MilitaryDiscountEnrollmentAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "email",
        "dob",
        "id_type",
        "created_at",
    )
    search_fields = ("full_name", "email")
    list_filter = ("id_type", "created_at")
