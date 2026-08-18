from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .csv_import import CsvImportError, import_sims
from .models import Order, Sim, SimReservation


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
        "iccid", "msisdn", "type_of_sim", "colored_status", "inventory",
        "order_reference", "prepaid_status",
        "subscriber", "customer_account", "first_activation_date", "last_seen_date",
    )
    list_display_links = ("iccid",)
    list_filter = (
        "inventory", "provisioning_status", "prepaid_status", "type_of_sim",
        "group", "customer_account",
    )
    search_fields = (
        "iccid", "imsi", "msisdn", "serial_number", "subscriber", "reference",
        "subscriber_number", "last_imei",
    )
    date_hierarchy = "first_activation_date"
    list_per_page = 50
    ordering = ("provisioning_status", "-first_activation_date")

    readonly_fields = (
        "imported_at", "activated_at", "activation_transaction_id",
        "esim_qr_preview", "esim_qr_fetched_at", "esim_qr_emailed",
        "psim_details_emailed",
    )
    fieldsets = (
        ("Identity", {
            "fields": ("iccid", "serial_number", "imsi", "msisdn", "type_of_sim", "sim_reference"),
        }),
        ("Status", {"fields": ("provisioning_status", "prepaid_status")}),
        ("Fulfilment", {
            "fields": ("inventory", "order_reference", "activated_at",
                       "activation_transaction_id"),
        }),
        ("eSIM QR / LPA", {
            "description": "Populated after activation, eSIMs only — physical SIMs never get a QR.",
            "fields": ("esim_qr_preview", "esim_qr_value", "esim_activation_code",
                       "esim_smdp_address", "esim_qr_status", "esim_qr_fetched_at",
                       "esim_qr_emailed"),
        }),
        ("pSIM details email", {
            "description": "ICCID/IMSI/MSISDN emailed to the order's billing_email, physical SIMs only.",
            "fields": ("psim_details_emailed",),
        }),
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

    @admin.display(description="QR code")
    def esim_qr_preview(self, obj):
        if not obj.esim_qr_image:
            return "—"
        return format_html(
            '<img src="{}" alt="eSIM QR" style="width:160px;height:160px;'
            'border:1px solid #ddd;border-radius:6px" />',
            obj.esim_qr_image.url,
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


@admin.register(SimReservation)
class SimReservationAdmin(admin.ModelAdmin):
    list_display = ("sim", "cart_key", "session_id", "expires_at", "created_at")
    list_filter = ("expires_at", "created_at")
    search_fields = ("cart_key", "session_id", "sim__iccid", "sim__msisdn")
    date_hierarchy = "expires_at"
    autocomplete_fields = ("sim",)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("order_reference", "email", "billing_email", "status", "country_code", "created_at", "updated_at")
    list_filter = ("status", "country_code", "created_at")
    search_fields = ("order_reference", "email", "billing_email", "user_id")
    date_hierarchy = "created_at"
    readonly_fields = (
        "order_reference", "email", "billing_email", "user_id", "country_code",
        "qr_codes_preview", "payload", "subscriber", "assigned", "errors",
        "created_at", "updated_at",
    )
    fieldsets = (
        ("Order", {"fields": ("order_reference", "status", "email", "billing_email", "user_id", "country_code")}),
        ("eSIM QR codes", {
            "description": "One panel per SIM in this order (from the `assigned` snapshot below). "
                            "Physical SIMs never get a QR — only eSIM lines show one.",
            "fields": ("qr_codes_preview",),
        }),
        ("Request", {"fields": ("payload", "subscriber")}),
        ("Result", {"fields": ("assigned", "errors")}),
        ("Bookkeeping", {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="eSIM QR codes")
    def qr_codes_preview(self, obj):
        assigned = obj.assigned or []
        if not assigned:
            return "No SIMs assigned to this order yet."

        iccids = [a.get("iccid") for a in assigned if a.get("iccid")]
        sims_by_iccid = {s.iccid: s for s in Sim.objects.filter(iccid__in=iccids)}

        panels = []
        for a in assigned:
            iccid = a.get("iccid", "") or "(no iccid)"
            msisdn = a.get("msisdn", "") or ""
            sim = sims_by_iccid.get(iccid)
            sim_type = (sim.sim_type_key if sim else a.get("sim_type", "")) or ""

            if sim_type != "esim":
                panels.append(format_html(
                    '<div style="display:inline-block;margin:8px;padding:14px 18px;'
                    'border:1px solid #e0e0e0;border-radius:8px;vertical-align:top;'
                    'background:#fafafa">'
                    '<div style="font-size:12px;color:#888;text-transform:uppercase;'
                    'letter-spacing:.03em">Physical SIM — no QR</div>'
                    '<code style="font-size:12px">{}</code><br/>'
                    '<span style="font-size:12px;color:#666">{}</span>'
                    "</div>",
                    iccid, msisdn or "no MSISDN",
                ))
                continue

            if sim and sim.esim_qr_image:
                panels.append(format_html(
                    '<div style="display:inline-block;margin:8px;padding:14px;'
                    'border:1px solid #ddd;border-radius:8px;text-align:center;'
                    'vertical-align:top">'
                    '<img src="{}" alt="eSIM QR" style="width:180px;height:180px;'
                    'border-radius:4px" /><br/>'
                    '<code style="font-size:11px">{}</code><br/>'
                    '<span style="font-size:12px;color:#666">{}</span>'
                    "</div>",
                    sim.esim_qr_image.url, iccid, msisdn or "",
                ))
            else:
                qr_status = sim.esim_qr_status if sim else ""
                panels.append(format_html(
                    '<div style="display:inline-block;margin:8px;padding:14px 18px;'
                    'max-width:260px;border-left:3px solid #ffc107;background:#fff3cd;'
                    'border-radius:4px;vertical-align:top">'
                    '<strong>eSIM {}</strong><br/>'
                    '<span style="font-size:13px;color:#665;">QR not generated yet{}. '
                    "Run <code>python manage.py send_esim_qr</code> to fetch it once "
                    "Transatel has provisioned the profile (usually a few seconds to a "
                    "couple of minutes after activation).</span>"
                    "</div>",
                    iccid, f" (status: {qr_status})" if qr_status else "",
                ))

        return mark_safe(
            '<div style="display:flex;flex-wrap:wrap;gap:4px">' + "".join(panels) + "</div>"
        )

    def has_add_permission(self, request):
        # Orders are only ever created by the checkout flow.
        return False