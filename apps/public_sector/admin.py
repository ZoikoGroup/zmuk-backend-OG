from django.contrib import admin
from django.db.models import Count

from .models import Nominee, PublicSectorRegistration


class NomineeInline(admin.TabularInline):
    model = Nominee
    extra = 0
    fields = ("full_name", "email", "phone", "relationship")


@admin.register(PublicSectorRegistration)
class PublicSectorRegistrationAdmin(admin.ModelAdmin):

    list_display = (
        "full_name",
        "email",
        "employer_name",
        "job_title",
        "status",
        "nominee_count",
        "email_sent",
        "created_at",
    )
    list_editable = ("status",)
    list_filter = ("status", "email_sent", "created_at")
    search_fields = (
        "full_name",
        "email",
        "work_email",
        "phone",
        "employer_name",
        "nominees__full_name",
        "nominees__email",
    )
    readonly_fields = ("email_sent", "created_at", "updated_at")
    date_hierarchy = "created_at"
    inlines = [NomineeInline]

    fieldsets = (
        ("Personal Details", {
            "fields": ("full_name", "email", "phone", "date_of_birth"),
        }),
        ("Employment Details", {
            "fields": ("employer_name", "job_title", "work_email", "department"),
        }),
        ("Declarations", {
            "fields": ("agree_terms", "is_public_sector_worker", "confirm_accurate"),
        }),
        ("Review", {
            "fields": ("status", "email_sent", "created_at", "updated_at"),
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_nominee_count=Count("nominees"))

    @admin.display(description="Nominees", ordering="_nominee_count")
    def nominee_count(self, obj):
        return obj._nominee_count