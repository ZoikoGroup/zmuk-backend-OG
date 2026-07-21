from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from .csv_import import CsvImportError, import_sims
from .models import Sim


class CsvImportForm(forms.Form):
    csv_file = forms.FileField(
        label="Park CSV file",
        help_text="The Transatel export (semicolon-delimited). Rows are matched on ICCID: "
                  "existing SIMs are updated, new ones are created.",
    )


@admin.register(Sim)
class SimAdmin(admin.ModelAdmin):
    change_list_template = "admin/sims/sim_changelist.html"

    list_display = (
        "iccid", "msisdn", "type_of_sim", "colored_status", "prepaid_status",
        "subscriber", "customer_account", "first_activation_date", "last_seen_date",
    )
    list_display_links = ("iccid",)
    list_filter = (
        "provisioning_status", "prepaid_status", "type_of_sim", "group", "customer_account",
    )
    search_fields = (
        "iccid", "imsi", "msisdn", "serial_number", "subscriber", "reference",
        "subscriber_number", "last_imei",
    )
    date_hierarchy = "first_activation_date"
    list_per_page = 50
    ordering = ("provisioning_status", "-first_activation_date")

    readonly_fields = ("imported_at",)
    fieldsets = (
        ("Identity", {
            "fields": ("iccid", "serial_number", "imsi", "msisdn", "type_of_sim", "sim_reference"),
        }),
        ("Status", {"fields": ("provisioning_status", "prepaid_status")}),
        ("Owner / account", {
            "fields": ("subscriber", "company", "reference", "customer_account", "group", "point_of_sale"),
        }),
        ("Dates", {"fields": ("first_activation_date", "last_action_date", "last_seen_date")}),
        ("Service", {
            "fields": ("provisioning_action_number", "subscriber_number", "service_pack",
                       "service_profile", "rate_plan"),
        }),
        ("Network / device", {
            "fields": ("last_imei", "last_origin_country", "last_mcc", "last_mnc",
                       "last_rat", "last_destination"),
        }),
        ("Usage", {"fields": ("data_usage_gb", "voice_usage", "sms_usage", "mms_usage")}),
        ("Bookkeeping", {"fields": ("imported_at",)}),
    )

    _STATUS_COLORS = {
        "Active": "#1a7f37",
        "Suspended": "#9a6700",
        "Terminated": "#b42318",
        "Available": "#57606a",
    }

    @admin.display(description="Provisioning status", ordering="provisioning_status")
    def colored_status(self, obj):
        color = self._STATUS_COLORS.get(obj.provisioning_status, "#57606a")
        return format_html(
            '<b style="color:{}">{}</b>', color, obj.provisioning_status or "—"
        )

    # --- CSV import wiring ---
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path("import-csv/", self.admin_site.admin_view(self.import_csv_view),
                 name="sims_sim_import_csv"),
        ]
        return custom + urls

    def import_csv_view(self, request):
        if request.method == "POST":
            form = CsvImportForm(request.POST, request.FILES)
            if form.is_valid():
                raw = form.cleaned_data["csv_file"].read()
                try:
                    with transaction.atomic():
                        result = import_sims(raw)
                except CsvImportError as exc:
                    self.message_user(request, str(exc), level=messages.ERROR)
                    return redirect("..")

                self.message_user(
                    request,
                    f"Import complete: {result['created']} created, "
                    f"{result['updated']} updated, {result['skipped']} skipped.",
                    level=messages.SUCCESS,
                )
                for err in result["errors"][:10]:
                    self.message_user(request, err, level=messages.WARNING)
                if len(result["errors"]) > 10:
                    self.message_user(
                        request,
                        f"...and {len(result['errors']) - 10} more row warnings.",
                        level=messages.WARNING,
                    )
                return redirect("..")
        else:
            form = CsvImportForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Import SIM park CSV",
            "form": form,
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "admin/sims/import_csv.html", context)
