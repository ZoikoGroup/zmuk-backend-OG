from django import forms
from django.contrib import admin, messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from .models import Destination, PlanMappingImport, PlanTariffMap, RegisterEntry, RegisterVersion, RoamingQuote
from .services import importer, plan_mapping, reports


class ReadOnlyAdmin(admin.ModelAdmin):
    """Imported or audit data: viewable by staff, never edited by hand."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class RegisterImportForm(forms.Form):
    label = forms.CharField(max_length=64, help_text="e.g. 2026-11-parity-r1")
    validation_date = forms.DateField(help_text="Date the data was checked against live tariff/billing records.")
    source_system = forms.CharField(max_length=128, required=False)
    notes = forms.CharField(widget=forms.Textarea, required=False)
    csv_file = forms.FileField(label="Register CSV")


class PlanMappingImportForm(forms.Form):
    csv_file = forms.FileField(label="Plan mapping CSV")
    notes = forms.CharField(widget=forms.Textarea, required=False)
    dry_run = forms.BooleanField(
        required=False, initial=True, label="Validate only (don't save)",
        help_text="Untick to apply the file once it validates with no problems.",
    )


def csv_download(file_name: str, columns, rows) -> HttpResponse:
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{file_name}"'
    reports.write_report(response, columns, rows)
    return response


@admin.register(Destination)
class DestinationAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "kind", "parent_code", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("code", "name", "aliases")


@admin.register(RegisterVersion)
class RegisterVersionAdmin(admin.ModelAdmin):
    """Upload Product/Ops' register export, review unresolved rows, then activate it."""

    change_list_template = "admin/roaming/registerversion/change_list.html"
    list_display = (
        "label", "status", "validation_date", "row_count", "unresolved_link", "report_link", "imported_by",
        "imported_at", "activated_at",
    )
    list_filter = ("status",)
    readonly_fields = (
        "label", "status", "checksum", "validation_date", "source_system", "row_count", "unresolved_count",
        "imported_by", "imported_at", "activated_by", "activated_at",
    )
    fields = readonly_fields + ("notes",)
    actions = ("activate_version",)

    def has_add_permission(self, request):
        return False  # versions are created by the CSV import only

    def has_delete_permission(self, request, obj=None):
        return obj is not None and obj.status == RegisterVersion.Status.DRAFT

    @admin.display(description="Unresolved rows")
    def unresolved_link(self, obj):
        url = reverse("admin:roaming_registerentry_changelist")
        return format_html(
            '<a href="{}?version__id__exact={}&row_status__exact=unresolved">{}</a>',
            url, obj.pk, obj.unresolved_count,
        )

    @admin.display(description="Rejection report")
    def report_link(self, obj):
        if not obj.unresolved_count:
            return "—"
        url = reverse("admin:roaming_registerversion_report", args=[obj.pk])
        return format_html('<a href="{}">Download CSV</a>', url)

    @admin.action(description="Activate selected version (retires the current one)")
    def activate_version(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Select exactly one version to activate.", messages.ERROR)
            return
        version = importer.activate(queryset.get(), user=request.user)
        self.message_user(request, f"'{version.label}' is now the active roaming register.", messages.SUCCESS)

    def get_urls(self):
        custom = [
            path("import-csv/", self.admin_site.admin_view(self.import_csv_view),
                 name="roaming_registerversion_import_csv"),
            path("<int:pk>/rejection-report/", self.admin_site.admin_view(self.report_view),
                 name="roaming_registerversion_report"),
        ]
        return custom + super().get_urls()

    def report_view(self, request, pk):
        version = get_object_or_404(RegisterVersion, pk=pk)
        return csv_download(
            f"roaming-register-{version.pk}-rejections.csv",
            reports.REGISTER_REPORT_COLUMNS, reports.register_report_rows(version),
        )

    def import_csv_view(self, request):
        if request.method == "POST":
            form = RegisterImportForm(request.POST, request.FILES)
            if form.is_valid():
                data = form.cleaned_data
                try:
                    version = importer.import_register(
                        data["csv_file"].read(), label=data["label"], validation_date=data["validation_date"],
                        source_system=data["source_system"], notes=data["notes"], user=request.user,
                    )
                except importer.RegisterImportError as exc:
                    self.message_user(request, str(exc), messages.ERROR)
                    return redirect("..")
                level = messages.WARNING if version.unresolved_count else messages.SUCCESS
                self.message_user(
                    request,
                    f"Imported '{version.label}' as a draft: {version.row_count} rows, "
                    f"{version.unresolved_count} unresolved. Review it, then use 'Activate'.",
                    level,
                )
                return redirect("..")
        else:
            form = RegisterImportForm()
        context = {
            **self.admin_site.each_context(request),
            "title": "Import roaming register CSV",
            "form": form,
            "opts": self.model._meta,
            "columns": importer.COLUMNS,
            "required_columns": importer.REQUIRED_COLUMNS,
        }
        return TemplateResponse(request, "admin/roaming/registerversion/import_csv.html", context)


@admin.register(RegisterEntry)
class RegisterEntryAdmin(ReadOnlyAdmin):
    list_display = (
        "row_number", "version", "plan_sku", "plan_version", "territory_code", "rating_zone",
        "supplier_availability", "inclusion", "effective_from", "effective_to", "row_status",
    )
    list_filter = ("version", "row_status", "inclusion", "supplier_availability", "rating_zone")
    search_fields = ("plan_sku", "territory_code", "source_record_id", "unresolved_reason")


@admin.register(PlanTariffMap)
class PlanTariffMapAdmin(admin.ModelAdmin):
    list_display = ("plan", "term", "plan_sku", "plan_version", "effective_from", "effective_to", "import_batch")
    list_filter = ("term", "import_batch")
    search_fields = ("plan__name", "plan_sku")
    raw_id_fields = ("plan",)
    readonly_fields = ("import_batch", "source_row")


@admin.register(PlanMappingImport)
class PlanMappingImportAdmin(admin.ModelAdmin):
    """Upload the plan-to-tariff mapping CSV. Deleting a batch removes the mappings it created (rollback)."""

    change_list_template = "admin/roaming/planmappingimport/change_list.html"
    list_display = ("imported_at", "file_name", "row_count", "created_count", "skipped_count", "imported_by")
    readonly_fields = (
        "checksum", "file_name", "row_count", "created_count", "skipped_count", "imported_by", "imported_at",
    )
    fields = readonly_fields + ("notes",)

    def has_add_permission(self, request):
        return False  # batches are created by the CSV import only

    def get_urls(self):
        custom = [
            path("import-csv/", self.admin_site.admin_view(self.import_csv_view),
                 name="roaming_planmappingimport_import_csv"),
        ]
        return custom + super().get_urls()

    def import_csv_view(self, request):
        result = None
        if request.method == "POST":
            form = PlanMappingImportForm(request.POST, request.FILES)
            if form.is_valid():
                data = form.cleaned_data
                upload = data["csv_file"]
                try:
                    result = plan_mapping.import_plan_mapping(
                        upload.read(), file_name=upload.name, notes=data["notes"], user=request.user,
                        dry_run=data["dry_run"],
                    )
                except plan_mapping.PlanMappingImportError as exc:
                    self.message_user(request, str(exc), messages.ERROR)
                    return redirect(".")
                if result.ok and not result.dry_run:
                    self.message_user(
                        request,
                        f"Applied: {result.created} mappings created, {result.skipped} already present.",
                        messages.SUCCESS,
                    )
                    return redirect("..")
        else:
            form = PlanMappingImportForm()
        context = {
            **self.admin_site.each_context(request),
            "title": "Import plan-to-tariff mapping CSV",
            "form": form,
            "result": result,
            "opts": self.model._meta,
            "columns": plan_mapping.COLUMNS,
            "required_columns": plan_mapping.REQUIRED_COLUMNS,
        }
        return TemplateResponse(request, "admin/roaming/planmappingimport/import_csv.html", context)


@admin.register(RoamingQuote)
class RoamingQuoteAdmin(ReadOnlyAdmin):
    list_display = ("uuid", "user", "line", "destination", "travel_date", "status", "outcome", "created_at")
    list_filter = ("status", "outcome")
    search_fields = ("uuid", "user__email", "line__current_msisdn", "destination__code")
