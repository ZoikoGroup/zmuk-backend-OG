from django.contrib import admin, messages

from .models import (
    AddLineOrder,
    CapacityHold,
    ContractAcceptance,
    Line,
    LineCapOverride,
    LineClaim,
    LineEvent,
    PortRequest,
    SupplierEvent,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Audit/log tables: viewable by staff, never edited or deleted by hand."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Line)
class LineAdmin(admin.ModelAdmin):
    list_display = (
        "current_msisdn", "owner", "status", "plan", "term", "source", "activated_at", "created_at",
    )
    list_filter = ("status", "source", "term")
    search_fields = (
        "current_msisdn", "original_msisdn", "owner__email", "owner__username",
        "sim__iccid", "supplier_subscriber_id",
    )
    raw_id_fields = ("owner", "sim", "plan")
    readonly_fields = ("uuid", "created_at", "updated_at")


@admin.register(CapacityHold)
class CapacityHoldAdmin(admin.ModelAdmin):
    list_display = ("uuid", "owner", "status", "expires_at", "release_reason", "created_at")
    list_filter = ("status",)
    search_fields = ("owner__email", "owner__username")
    raw_id_fields = ("owner",)
    readonly_fields = ("uuid", "created_at", "updated_at")


@admin.register(LineCapOverride)
class LineCapOverrideAdmin(admin.ModelAdmin):
    list_display = ("owner", "max_lines", "reason", "set_by", "updated_at")
    search_fields = ("owner__email", "owner__username", "reason")
    raw_id_fields = ("owner",)
    readonly_fields = ("set_by", "created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        obj.set_by = request.user
        super().save_model(request, obj, form, change)


class ContractAcceptanceInline(admin.StackedInline):
    model = ContractAcceptance
    can_delete = False
    extra = 0
    readonly_fields = (
        "contract_version", "contract_summary_version", "documents_hash", "accepted_at",
        "immediate_service_requested_at", "ip_address", "user_agent",
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(AddLineOrder)
class AddLineOrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_reference", "owner", "plan", "term", "sim_type", "switch_choice",
        "total_gross", "payment_status", "fulfilment_status", "created_at",
    )
    list_filter = ("payment_status", "fulfilment_status", "sim_type", "switch_choice", "term")
    search_fields = ("order_reference", "owner__email", "stripe_payment_intent_id")
    raw_id_fields = ("owner", "hold", "line", "reserved_sim", "sim_order", "plan", "coupon")
    readonly_fields = (
        "order_reference", "quote", "quote_hash", "total_gross", "vat_amount", "billing",
        "stripe_payment_intent_id", "paid_at", "last_payment_error", "payment_failed_at",
        "created_at", "updated_at",
    )
    inlines = [ContractAcceptanceInline]


@admin.register(PortRequest)
class PortRequestAdmin(admin.ModelAdmin):
    list_display = (
        "line", "code_type", "portability_msisdn", "status", "port_date",
        "supplier_transaction_id", "submitted_at", "completed_at",
    )
    list_filter = ("status", "code_type")
    search_fields = ("portability_msisdn", "supplier_transaction_id", "line__current_msisdn")
    raw_id_fields = ("line", "order")
    # The PAC/STAC itself is deliberately excluded from the form.
    exclude = ("encrypted_code",)
    readonly_fields = (
        "uuid", "code_purged_at", "supplier_transaction_id", "supplier_request_id",
        "last_error", "submitted_at", "validated_at", "locked_at", "completed_at",
        "created_at", "updated_at",
    )


@admin.register(SupplierEvent)
class SupplierEventAdmin(ReadOnlyAdmin):
    list_display = ("event_type", "event_id", "status", "port_request", "received_at", "processed_at")
    list_filter = ("status", "event_type")
    search_fields = ("event_id", "event_type")


@admin.register(LineClaim)
class LineClaimAdmin(admin.ModelAdmin):
    """Support queue for "Claim your existing SIM" (A1).

    Check the evidence, set `matched_sim` if it wasn't found automatically,
    then use the Approve / Reject actions so the decision is audited.
    """

    list_display = ("owner", "iccid", "msisdn", "order_reference", "status", "reviewed_by", "created_at")
    list_filter = ("status",)
    search_fields = ("owner__email", "iccid", "msisdn", "order_reference")
    raw_id_fields = ("owner", "matched_sim", "matched_order", "resulting_line", "reviewed_by")
    readonly_fields = ("evidence", "status", "resulting_line", "reviewed_by", "reviewed_at", "created_at")
    actions = ("approve_claims", "reject_claims")

    @admin.action(description="Approve selected claims (link SIM to the customer)")
    def approve_claims(self, request, queryset):
        from .services import claims

        for claim in queryset:
            try:
                claims.approve(claim, reviewer=request.user, note=claim.decision_note or "approved by support")
                self.message_user(request, f"Claim {claim.pk}: SIM linked.", messages.SUCCESS)
            except claims.ClaimError as exc:
                self.message_user(request, f"Claim {claim.pk}: {exc.message}", messages.ERROR)

    @admin.action(description="Reject selected claims")
    def reject_claims(self, request, queryset):
        from .services import claims

        rejected = sum(
            claims.reject(c, reviewer=request.user, note=c.decision_note or "rejected by support") for c in queryset
        )
        self.message_user(request, f"Rejected {rejected} claim(s).", messages.SUCCESS)


@admin.register(LineEvent)
class LineEventAdmin(ReadOnlyAdmin):
    list_display = ("kind", "status_before", "status_after", "line", "order", "actor", "created_at")
    list_filter = ("kind",)
    search_fields = ("line__current_msisdn", "order__order_reference")
