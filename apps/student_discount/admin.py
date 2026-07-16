from django.contrib import admin
from .models import StudentDiscountApplication


@admin.register(StudentDiscountApplication)
class StudentDiscountAdmin(admin.ModelAdmin):

    list_display = (
        "full_name",
        "email",
        "mobile",
        "selected_plan",
        "contract_duration",
        "status",
        "created_at",
    )

    list_filter = (
        "status",
        "selected_plan",
        "created_at",
    )

    search_fields = (
        "full_name",
        "email",
        "student_id_number",
        "institution",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    ordering = ("-created_at",)