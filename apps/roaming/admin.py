from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from .models import Destination, PlanTariffMap, RegisterEntry, RegisterVersion, RoamingQuote
from .services import importer


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
        "label", "status", "validation_date", "row_count", "unresolved_link", "imported_by", "imported_at",
        "activated_at",
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
        ]
        return custom + super().get_urls()

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
    list_display = ("plan", "term", "plan_sku", "plan_version", "effective_from", "effective_to")
    list_filter = ("term",)
    search_fields = ("plan__name", "plan_sku")
    raw_id_fields = ("plan",)


@admin.register(RoamingQuote)
class RoamingQuoteAdmin(ReadOnlyAdmin):
    list_display = ("uuid", "user", "line", "destination", "travel_date", "status", "outcome", "created_at")
    list_filter = ("status", "outcome")
    search_fields = ("uuid", "user__email", "line__current_msisdn", "destination__code")
