import mimetypes
import os
import threading

from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import StudentDiscountApplication
from .services import send_decision_email

DECIDED = ("approved", "rejected")
OPEN = StudentDiscountApplication.OPEN_STATUSES


class ApplicationAdminForm(forms.ModelForm):
    """Explains the one-open-application rule instead of failing on save.

    On the full form Django checks the constraint itself; the list view's
    status-only form leaves out `user`, so Django skips it and we check here.
    """

    class Meta:
        model = StudentDiscountApplication
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if "user" in self.fields or not self.instance.user_id:
            return cleaned
        status = cleaned.get("status", self.instance.status)
        if status in OPEN and (
            StudentDiscountApplication.objects
            .filter(user_id=self.instance.user_id, status__in=OPEN)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError(
                "This student already has another open (pending, under review "
                "or approved) application."
            )
        return cleaned


@admin.register(StudentDiscountApplication)
class StudentDiscountAdmin(admin.ModelAdmin):

    form = ApplicationAdminForm

    list_display = (
        "full_name",
        "email",
        "user",
        "mobile",
        "selected_plan",
        "contract_duration",
        "graduation_date",
        "status",
        "created_at",
    )

    list_editable = ("status",)

    list_filter = (
        "status",
        "selected_plan",
        "created_at",
    )

    search_fields = (
        "full_name",
        "email",
        "user__email",
        "student_id_number",
        "institution",
    )

    raw_id_fields = ("user",)

    # The file itself is private: shown as a link to the admin-only view below.
    exclude = ("student_id_document",)

    readonly_fields = (
        "student_id_document_link",
        "reviewed_at",
        "created_at",
        "updated_at",
    )

    ordering = ("-created_at",)

    def get_changelist_form(self, request, **kwargs):
        kwargs.setdefault("form", ApplicationAdminForm)
        return super().get_changelist_form(request, **kwargs)

    # ── Private document ──

    def get_urls(self):
        return [
            path(
                "<int:pk>/document/",
                self.admin_site.admin_view(self.document_view),
                name="student_discount_document",
            ),
        ] + super().get_urls()

    def document_view(self, request, pk):
        application = get_object_or_404(StudentDiscountApplication, pk=pk)
        if not self.has_view_permission(request, application):
            raise PermissionDenied

        document = application.student_id_document
        if not document:
            raise Http404("No document uploaded.")
        try:
            handle = document.open("rb")
        except (FileNotFoundError, OSError):
            raise Http404("The document file is missing.")

        ext = os.path.splitext(document.name)[1]
        response = FileResponse(
            handle,
            content_type=mimetypes.guess_type(document.name)[0] or "application/octet-stream",
            filename=f"student-id-{application.pk}{ext}",
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    @admin.display(description="Student ID document")
    def student_id_document_link(self, obj):
        if not obj.pk or not obj.student_id_document:
            return "No document uploaded"
        url = reverse("admin:student_discount_document", args=[obj.pk])
        return format_html('<a href="{}" target="_blank" rel="noopener">Open document</a>', url)

    # ── Decision ──

    def save_model(self, request, obj, form, change):
        status_changed = change and "status" in form.changed_data
        if status_changed:
            obj.reviewed_at = timezone.now() if obj.status in DECIDED else None
        super().save_model(request, obj, form, change)

        # Email the student once the decision is saved.
        if status_changed and obj.status in DECIDED:
            transaction.on_commit(
                lambda: threading.Thread(
                    target=send_decision_email, args=[obj.pk], daemon=True
                ).start()
            )
