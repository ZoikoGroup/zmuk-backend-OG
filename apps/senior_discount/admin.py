from django.contrib import admin
from .models import SeniorCitizenDiscount

@admin.register(SeniorCitizenDiscount)
class SeniorCitizenDiscountAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "email",
        "dob",
        "id_type",
        "created_at",
    )
    search_fields = ("full_name", "email")
