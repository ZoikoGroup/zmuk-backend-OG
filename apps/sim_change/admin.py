"""Admin for Change SIM type: read-only records plus the staff work queue.

Rows are never edited directly. Staff steps (verification, approval, stock,
dispatch, authorise, execute, holds) are buttons on a case that open a short
form and call `services.operations`, which checks the person's role and the
separation-of-duties rules. Nothing here can confirm a swap: only a supplier
read can.
"""

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from . import admin_actions
from .models import (
    LineOperationLock, ProviderCommand, SimChangePlanEligibility, SimTypeChangeCase, SimTypeChangeEvent,
    StatusSource, StepUpGrant, SupplierVerificationEvidence, SwapSimStock,
)
from .services import address, operations
from .services.transitions import TransitionError


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class EventInline(admin.TabularInline):
    model = SimTypeChangeEvent
    fields = ("created_at", "kind", "status_before", "status_after", "status_source", "actor", "reason")
    readonly_fields = fields
    extra = 0
    can_delete = False
    ordering = ("created_at", "id")

    def has_add_permission(self, request, obj=None):
        return False


class WorkQueueFilter(admin.SimpleListFilter):
    title = "work queue"
    parameter_name = "queue"
    SKIP = ("cancel", "delivery_exception")

    def lookups(self, request, model_admin):
        return [(key, a.label) for key, a in admin_actions.BY_KEY.items() if key not in self.SKIP]

    def queryset(self, request, queryset):
        action = admin_actions.BY_KEY.get(self.value())
        if action is None:
            return queryset
        open_cases = queryset.exclude(status__in=SimTypeChangeCase.TERMINAL_STATUSES)
        return queryset.filter(pk__in=[c.pk for c in open_cases if action.available(c)])


@admin.register(SimTypeChangeCase)
class SimTypeChangeCaseAdmin(ReadOnlyAdmin):
    list_display = (
        "uuid", "owner", "direction", "status", "physical_fulfilment", "esim_fulfilment", "action_owner",
        "risk", "version", "created_at",
    )
    list_filter = (WorkQueueFilter, "status", "direction", "action_owner", "physical_fulfilment", "esim_fulfilment")
    search_fields = ("uuid", "owner__email", "owner__username")
    # Encrypted values and fingerprints are not shown.
    exclude = ("delivery_snapshot_encrypted", "delivery_address_fingerprint")
    inlines = [EventInline]
    change_form_template = "admin/sim_change/simtypechangecase/change_form.html"

    @admin.display(description="Risk")
    def risk(self, obj):
        return operations.case_risk_tier(obj)

    def get_urls(self):
        custom = [
            path("<uuid:case_id>/action/<str:action>/", self.admin_site.admin_view(self.action_view),
                 name="sim_change_case_action"),
        ]
        return custom + super().get_urls()

    def change_view(self, request, object_id, form_url="", extra_context=None):
        case = self.get_object(request, object_id)
        extra_context = extra_context or {}
        if case is not None:
            extra_context["sim_change_actions"] = [
                {"label": a.label, "url": reverse("admin:sim_change_case_action", args=[case.uuid, a.key])}
                for a in admin_actions.actions_for(request.user, case)
            ]
            extra_context["sim_change_risk"] = {"tier": operations.case_risk_tier(case), "flags": case.risk_flags}
        return super().change_view(request, object_id, form_url, extra_context)

    def action_view(self, request, case_id, action):
        case = get_object_or_404(SimTypeChangeCase, uuid=case_id)
        spec = admin_actions.BY_KEY.get(action)
        if spec is None or not admin_actions.permitted(request.user, spec, case):
            raise PermissionDenied
        case_url = reverse("admin:sim_change_simtypechangecase_change", args=[case.pk])
        if not spec.available(case):
            messages.warning(request, "That step isn't available for this request any more.")
            return redirect(case_url)

        form = admin_actions.make_form(spec, case, request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                message = spec.run(case, request.user, form.cleaned_data)
            except TransitionError as exc:
                form.add_error(None, f"{exc} ({exc.code})")
            else:
                messages.success(request, message)
                return redirect(case_url)

        delivery = None
        if spec.shows_address and case.delivery_snapshot_encrypted:
            # Access to the decrypted address is audited.
            delivery = address.delivery_address(case)
            SimTypeChangeEvent.objects.create(
                tenant=case.tenant, case=case, actor=request.user, actor_role="staff", kind="delivery_address_viewed",
                status_source=StatusSource.SUPPORT, version=case.version, detail={"action": spec.key},
            )
        context = {
            **self.admin_site.each_context(request),
            "title": spec.label,
            "opts": self.model._meta,
            "case": case,
            "case_url": case_url,
            "form": form,
            "action": spec,
            "delivery_address": delivery,
            "risk": {"tier": operations.case_risk_tier(case), "flags": case.risk_flags},
        }
        return TemplateResponse(request, "admin/sim_change/simtypechangecase/action_form.html", context)


@admin.register(ProviderCommand)
class ProviderCommandAdmin(ReadOnlyAdmin):
    list_display = (
        "uuid", "case", "operation", "channel", "status", "authorised_by", "executed_by", "attempts", "created_at",
    )
    list_filter = ("status", "channel")


@admin.register(SupplierVerificationEvidence)
class SupplierVerificationEvidenceAdmin(ReadOnlyAdmin):
    list_display = ("case", "source", "binding_confirmed", "supplier_status", "fetched_at")
    exclude = ("target_serial_fingerprint", "old_serial_fingerprint")


@admin.register(StepUpGrant)
class StepUpGrantAdmin(ReadOnlyAdmin):
    list_display = (
        "uuid", "case", "subject", "purpose", "verification_method", "assurance_result", "verified_by",
        "approved_by", "issued_at", "expires_at", "consumed_at", "revoked_at",
    )
    list_filter = ("purpose", "verification_method", "assurance_result")


@admin.register(LineOperationLock)
class LineOperationLockAdmin(ReadOnlyAdmin):
    list_display = ("line", "operation", "reference", "acquired_at", "released_at")
    list_filter = ("operation",)


@admin.register(SwapSimStock)
class SwapSimStockAdmin(ReadOnlyAdmin):
    list_display = ("sim", "sim_format", "status", "supplier_status", "supplier_pool_id", "reserved_case",
                    "reservation_expires_at", "source", "imported_at")
    list_filter = ("status", "sim_format", "supplier_pool_id")
    search_fields = ("sim__iccid",)
    actions = ("quarantine_selected",)

    def has_quarantine_permission(self, request):
        return request.user.has_perm(operations.PERM["stock"])

    @admin.action(description="Quarantine selected Swap SIMs (needs a Network Ops check)", permissions=["quarantine"])
    def quarantine_selected(self, request, queryset):
        done = 0
        for row in queryset:
            try:
                operations.quarantine_stock(row, actor=request.user, reason=f"Quarantined from the admin by {request.user}")
                done += 1
            except TransitionError as exc:
                messages.warning(request, f"{row}: {exc}")
        messages.success(request, f"Quarantined {done} Swap SIM(s).")


@admin.register(SimChangePlanEligibility)
class SimChangePlanEligibilityAdmin(admin.ModelAdmin):
    """Product enables plans per direction once Commercial has approved (v2.1 B5).

    Rows are versioned: add a new version rather than editing an old one.
    """

    list_display = ("plan", "version", "enabled", "psim_to_esim", "esim_to_psim", "effective_from", "approved_by")
    list_filter = ("enabled",)
    fields = ("plan", "version", "enabled", "psim_to_esim", "esim_to_psim", "effective_from",
              "approval_reference", "notes", "approved_by", "created_at")
    readonly_fields = ("approved_by", "created_at")

    def get_readonly_fields(self, request, obj=None):
        if obj is not None:  # existing versions are frozen
            return self.fields
        return self.readonly_fields

    def save_model(self, request, obj, form, change):
        if not change:
            obj.approved_by = request.user
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        return False
