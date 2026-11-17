import threading

from django import forms
from django.contrib import admin
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .models import Nominee, PublicSectorRegistration
from .services import send_decision_email

DECIDED = (PublicSectorRegistration.Status.APPROVED, PublicSectorRegistration.Status.REJECTED)


class RegistrationAdminForm(forms.ModelForm):
    """Explains the one-open-registration rule instead of failing on save.

    On the full form Django checks the constraint itself; the list view's
    status-only form leaves out `user`, so Django skips it and we check here.
    """

    class Meta:
        model = PublicSectorRegistration
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if "user" in self.fields or not self.instance.user_id:
            return cleaned
        status = cleaned.get("status", self.instance.status)
        if status in PublicSectorRegistration.OPEN_STATUSES and (
            PublicSectorRegistration.objects
            .filter(user_id=self.instance.user_id, status__in=PublicSectorRegistration.OPEN_STATUSES)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError(
                "This customer already has another pending or approved registration."
            )
        return cleaned


class NomineeInline(admin.TabularInline):
    model = Nominee
    extra = 0
    fields = ("full_name", "email", "phone", "relationship")


@admin.register(PublicSectorRegistration)
class PublicSectorRegistrationAdmin(admin.ModelAdmin):

    form = RegistrationAdminForm
    list_display = (
        "full_name",
        "email",
        "user",
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
        "user__email",
        "nominees__full_name",
        "nominees__email",
    )
    raw_id_fields = ("user",)
    readonly_fields = ("email_sent", "reviewed_at", "created_at", "updated_at")
    date_hierarchy = "created_at"
    inlines = [NomineeInline]

    fieldsets = (
        ("Account", {
            "fields": ("user",),
        }),
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
            "fields": (
                "status",
                "rejection_reason",
                "reviewed_at",
                "email_sent",
                "created_at",
                "updated_at",
            ),
        }),
    )

    def get_changelist_form(self, request, **kwargs):
        kwargs.setdefault("form", RegistrationAdminForm)
        return super().get_changelist_form(request, **kwargs)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_nominee_count=Count("nominees"))

    @admin.display(description="Nominees", ordering="_nominee_count")
    def nominee_count(self, obj):
        return obj._nominee_count

    def save_model(self, request, obj, form, change):
        status_changed = change and "status" in form.changed_data
        if status_changed:
            obj.reviewed_at = timezone.now() if obj.status in DECIDED else None
        super().save_model(request, obj, form, change)

        # Email the customer once the decision is saved.
        if status_changed and obj.status in DECIDED:
            transaction.on_commit(
                lambda: threading.Thread(
                    target=send_decision_email, args=[obj.pk], daemon=True
                ).start()
            )
