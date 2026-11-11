import re

from rest_framework import serializers

from apps.add_line.models import SimType, Term
from apps.add_line.services.quote import QuoteError, build_quote

from . import services
from .models import SwitchFeedback, SwitchRequest

# UK mobile: 07xxxxxxxxx or +447xxxxxxxxx
UK_MOBILE_RE = re.compile(r"^(?:\+44|0)7\d{9}$")

# Older frontend builds send "Other"; store it as the catch-all.
_PROVIDER_ALIASES = {"Other": "Other UK Provider"}

_QUOTE_ERROR_FIELD = {
    "PLAN_NOT_AVAILABLE": "plan_id",
    "TERM_INVALID": "contract_term",
    "SIM_TYPE_INVALID": "sim_type",
}


class StatusFieldsMixin(serializers.Serializer):
    """Status block shared by the submit response and the tracking endpoints."""

    status_label = serializers.CharField(source="get_status_display", read_only=True)
    milestone = serializers.SerializerMethodField()
    milestones = serializers.SerializerMethodField()
    pac_status = serializers.SerializerMethodField()
    estimated_completion_date = serializers.SerializerMethodField()
    history = serializers.SerializerMethodField()

    def _steps(self, obj):
        cache = self.context.setdefault("_milestones", {})
        if obj.pk not in cache:
            cache[obj.pk] = services.milestones(obj)
        return cache[obj.pk]

    def get_milestone(self, obj):
        return services.current_milestone(self._steps(obj))

    def get_milestones(self, obj):
        return self._steps(obj)

    def get_pac_status(self, obj):
        return services.pac_status(obj)

    def get_estimated_completion_date(self, obj):
        value = services.estimated_completion_date(obj)
        return value.isoformat() if value else None

    def get_history(self, obj):
        return services.history(obj)


class SwitchRequestSerializer(StatusFieldsMixin, serializers.ModelSerializer):
    """POST /api/switch-requests/ input and response `data`."""

    current_provider = serializers.CharField(max_length=50, required=False, allow_blank=True)
    plan_id = serializers.IntegerField(write_only=True)
    contract_term = serializers.ChoiceField(choices=Term.choices)
    sim_type = serializers.ChoiceField(choices=SimType.choices)
    switch_route = serializers.ChoiceField(choices=SwitchRequest.Route.choices)
    # The number being moved (defaults to `mobile`); echoed back masked only.
    port_number = serializers.CharField(max_length=24, required=False, allow_blank=True, write_only=True)
    # Accepted on input only: stored encrypted, never sent back (decision C13).
    pac_code = serializers.CharField(max_length=20, required=False, allow_blank=True, write_only=True)
    stac_code = serializers.CharField(max_length=20, required=False, allow_blank=True, write_only=True)
    promo_code = serializers.CharField(max_length=50, required=False, allow_blank=True)
    plan = serializers.SerializerMethodField()
    port_number_masked = serializers.SerializerMethodField()

    class Meta:
        model = SwitchRequest
        fields = [
            "id",
            "reference",
            # Step 1
            "first_name",
            "last_name",
            "email",
            "mobile",
            "postcode",
            # Step 2 (optional)
            "current_provider",
            "current_plan_cost",
            "current_data_allowance",
            # Step 3
            "plan_id",
            "plan",
            "selected_plan",
            "contract_term",
            "sim_type",
            "promo_code",
            # Step 4
            "switch_route",
            "port_number",
            "port_number_masked",
            "pac_code",
            "stac_code",
            "pac_later",
            "code_type",
            # Consent
            "transfer_authorised",
            "timeline_acknowledged",
            "terms_accepted",
            "opt_in_news",
            "opt_in_texts",
            # Status (read-only)
            "status",
            "status_label",
            "milestone",
            "milestones",
            "pac_status",
            "estimated_completion_date",
            "history",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id", "reference", "selected_plan", "code_type", "status", "created_at", "updated_at",
        ]

    def get_plan(self, obj):
        return {"id": obj.plan_id, "name": obj.selected_plan} if obj.plan_id else None

    def get_port_number_masked(self, obj):
        return services.mask_number(obj.port_number)

    # ── Step 1 validation ─────────────────────────────────────────────────
    def validate_first_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("First name is required.")
        return value

    def validate_last_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Last name is required.")
        return value

    def validate_mobile(self, value):
        cleaned = re.sub(r"[\s-]", "", value)
        if not UK_MOBILE_RE.match(cleaned):
            raise serializers.ValidationError("Enter a valid UK mobile number.")
        return cleaned

    def validate_postcode(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Billing address / postcode is required.")
        return value

    # ── Step 2 validation (optional, but if given must be sensible) ────────
    def validate_current_provider(self, value):
        value = _PROVIDER_ALIASES.get(value.strip(), value.strip())
        if value and value not in dict(SwitchRequest.PROVIDER_CHOICES):
            raise serializers.ValidationError("Choose your current provider from the list.")
        return value

    def validate_current_plan_cost(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Plan cost cannot be negative.")
        return value

    def validate_current_data_allowance(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Data allowance cannot be negative.")
        return value

    # ── Authorisation: each box must be ticked ────────────────────────────
    def validate_transfer_authorised(self, value):
        if not value:
            raise serializers.ValidationError(
                "You must confirm you are authorised to request this transfer."
            )
        return value

    def validate_timeline_acknowledged(self, value):
        if not value:
            raise serializers.ValidationError(
                "You must acknowledge the switch timeline."
            )
        return value

    def validate_terms_accepted(self, value):
        if not value:
            raise serializers.ValidationError(
                "You must agree to the Privacy Policy and Terms & Conditions."
            )
        return value

    # ── Cross-field: plan/promo, route and codes ──────────────────────────
    def validate(self, attrs):
        errors = {}
        route = attrs["switch_route"]
        needed = SwitchRequest.ROUTE_CODE[route]
        codes = {
            SwitchRequest.CodeType.PAC: (attrs.pop("pac_code", "") or "").strip(),
            SwitchRequest.CodeType.STAC: (attrs.pop("stac_code", "") or "").strip(),
        }
        field_for = {SwitchRequest.CodeType.PAC: "pac_code", SwitchRequest.CodeType.STAC: "stac_code"}

        # Codes that don't belong to the chosen route.
        for code_type, code in codes.items():
            if code and code_type != needed:
                errors[field_for[code_type]] = [
                    "No code is needed when you keep your old service." if needed is None
                    else f"Your switch option needs a {needed}, not a {code_type}."
                ]

        code = codes.get(needed, "") if needed else ""
        if needed is None:
            attrs["pac_later"] = False
        elif code:
            try:
                attrs["_code"] = (needed, services.normalise_code(needed, code))
            except services.PacError as exc:
                errors[field_for[needed]] = [exc.message]
            attrs["pac_later"] = False
        elif not attrs.get("pac_later"):
            errors[field_for[needed]] = [
                f"Enter your {needed}, or choose to provide it later."
            ]

        # The number being moved: given explicitly, else the contact mobile.
        number = (attrs.get("port_number") or "").strip()
        if route == SwitchRequest.Route.KEEP_PAC or number:
            try:
                attrs["port_number"] = services.normalise_msisdn(number or attrs.get("mobile", ""))
            except services.PacError as exc:
                errors["port_number"] = [exc.message]

        # Plan and promo, validated by the same pricing used at checkout.
        request = self.context.get("request")
        user = request.user if request and request.user.is_authenticated else None
        promo = (attrs.get("promo_code") or "").strip()
        attrs["promo_code"] = promo
        try:
            quote = build_quote(
                user, attrs["plan_id"], attrs["contract_term"], attrs["sim_type"], promo,
                additional_line=False,
            )
            attrs["_plan"] = quote.plan
        except QuoteError as exc:
            errors[_QUOTE_ERROR_FIELD.get(exc.code, "promo_code")] = [exc.message]

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def create(self, validated_data):
        validated_data.pop("plan_id")
        plan = validated_data.pop("_plan")
        code = validated_data.pop("_code", None)
        request = self.context.get("request")

        instance = SwitchRequest(
            **validated_data,
            plan=plan,
            selected_plan=plan.name,
            user=request.user if request and request.user.is_authenticated else None,
        )
        if code:
            # Raises CodeStorageUnavailable before anything is saved.
            services.apply_code(instance, *code)
        instance.save()
        if code:
            services.record_code_received(instance)
        return instance


class SwitchStatusSerializer(StatusFieldsMixin, serializers.ModelSerializer):
    """Tracking view: no personal data beyond the first name and a masked number."""

    plan = serializers.SerializerMethodField()
    port_number_masked = serializers.SerializerMethodField()
    order_reference = serializers.SerializerMethodField()
    line_id = serializers.SerializerMethodField()
    feedback_submitted = serializers.SerializerMethodField()

    class Meta:
        model = SwitchRequest
        fields = [
            "id",
            "reference",
            "first_name",
            "plan",
            "contract_term",
            "sim_type",
            "switch_route",
            "port_number_masked",
            "pac_later",
            "code_type",
            "status",
            "status_label",
            "milestone",
            "milestones",
            "pac_status",
            "estimated_completion_date",
            "history",
            "order_reference",
            "line_id",
            "feedback_submitted",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_plan(self, obj):
        return {"id": obj.plan_id, "name": obj.selected_plan} if obj.selected_plan else None

    def get_port_number_masked(self, obj):
        return services.mask_number(obj.port_number)

    def get_order_reference(self, obj):
        return obj.order.order_reference if obj.order_id else None

    def get_line_id(self, obj):
        return str(obj.line.uuid) if obj.line_id else None

    def get_feedback_submitted(self, obj):
        return hasattr(obj, "feedback")


class PortCodeSerializer(serializers.Serializer):
    """POST /api/switch-requests/<reference>/port-code/"""

    code_type = serializers.ChoiceField(choices=SwitchRequest.CodeType.choices)
    code = serializers.CharField(max_length=20)
    msisdn = serializers.CharField(max_length=24, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)


class FeedbackSerializer(serializers.ModelSerializer):
    """POST /api/switch-requests/<reference>/feedback/"""

    email = serializers.EmailField(required=False, allow_blank=True, write_only=True)

    class Meta:
        model = SwitchFeedback
        fields = ["rating", "comments", "email", "created_at"]
        read_only_fields = ["created_at"]
        extra_kwargs = {"comments": {"required": False, "allow_blank": True}}


class PublicQuoteSerializer(serializers.Serializer):
    """POST /api/v1/switch/quote/"""

    plan_id = serializers.IntegerField()
    term = serializers.ChoiceField(choices=Term.choices)
    sim_type = serializers.ChoiceField(choices=SimType.choices)
    promo_code = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
