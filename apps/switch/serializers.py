import re

from django.conf import settings
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


def consent_version() -> str:
    """Version of the Step 4 authorisation wording the customer agrees to."""
    return settings.SWITCH_CONSENT_VERSION


# ── Status views ──────────────────────────────────────────────────────────────

class GuestStatusSerializer(serializers.ModelSerializer):
    """Reference + email lookup: status only, no personal data (M04 §04)."""

    # Customer-facing: an Operations-only action shows as "validating".
    status = serializers.SerializerMethodField()
    status_label = serializers.SerializerMethodField()
    stage = serializers.SerializerMethodField()
    steps = serializers.SerializerMethodField()
    action_needed = serializers.SerializerMethodField()
    next_action = serializers.SerializerMethodField()
    pac_status = serializers.SerializerMethodField()
    switch_date = serializers.SerializerMethodField()
    switch_date_message = serializers.SerializerMethodField()
    history = serializers.SerializerMethodField()
    feedback_submitted = serializers.SerializerMethodField()

    class Meta:
        model = SwitchRequest
        fields = [
            "reference",
            "status",
            "status_label",
            "stage",
            "steps",
            "action_needed",
            "next_action",
            "switch_route",
            "code_type",
            "pac_status",
            "switch_date",
            "switch_date_message",
            "history",
            "feedback_submitted",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def _tracker(self, obj):
        cache = self.context.setdefault("_tracker", {})
        if obj.pk not in cache:
            cache[obj.pk] = services.tracker(obj)
        return cache[obj.pk]

    def get_status(self, obj):
        return services.customer_status(obj)[0]

    def get_status_label(self, obj):
        return services.customer_status(obj)[1]

    def get_stage(self, obj):
        return self._tracker(obj)["stage"]

    def get_steps(self, obj):
        return self._tracker(obj)["steps"]

    def get_action_needed(self, obj):
        return services.action_needed(obj)

    def get_next_action(self, obj):
        return services.next_action(obj)

    def get_pac_status(self, obj):
        return services.pac_status(obj)

    def get_switch_date(self, obj):
        value = services.switch_date(obj)
        return value.isoformat() if value else None

    def get_switch_date_message(self, obj):
        if obj.status in SwitchRequest.TERMINAL_STATUSES or services.switch_date(obj):
            return None
        return services.NO_DATE_MESSAGE

    def get_history(self, obj):
        return services.history(obj)

    def get_feedback_submitted(self, obj):
        return hasattr(obj, "feedback")


class OwnerStatusSerializer(GuestStatusSerializer):
    """The signed-in owner (or the submitter, in the submit response)."""

    plan = serializers.SerializerMethodField()
    port_number_masked = serializers.SerializerMethodField()
    order_reference = serializers.SerializerMethodField()
    line_id = serializers.SerializerMethodField()

    class Meta(GuestStatusSerializer.Meta):
        fields = ["id", "first_name"] + GuestStatusSerializer.Meta.fields + [
            "plan",
            "contract_term",
            "sim_type",
            "port_number_masked",
            "pac_later",
            "order_reference",
            "line_id",
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


class StaffSwitchRequestSerializer(serializers.ModelSerializer):
    """Staff-only list/detail. The encrypted code is never included."""

    class Meta:
        model = SwitchRequest
        exclude = ["encrypted_code"]


# ── Submit ────────────────────────────────────────────────────────────────────

class SwitchRequestSerializer(serializers.ModelSerializer):
    """POST /api/switch-requests/ input. The response uses OwnerStatusSerializer."""

    # Step 3: contact number is optional; the address is required.
    mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)
    address_line_1 = serializers.CharField(max_length=255, error_messages={
        "required": "Enter the first line of your address.",
        "blank": "Enter the first line of your address.",
    })
    address_line_2 = serializers.CharField(max_length=255, required=False, allow_blank=True)
    town_city = serializers.CharField(max_length=100, error_messages={
        "required": "Enter your town or city.",
        "blank": "Enter your town or city.",
    })
    postcode = serializers.CharField(max_length=16, error_messages={
        "required": "Enter your postcode.",
        "blank": "Enter your postcode.",
        "max_length": "Enter a valid UK postcode.",
    })
    current_provider = serializers.CharField(max_length=50, required=False, allow_blank=True)
    plan_id = serializers.IntegerField()
    contract_term = serializers.ChoiceField(choices=Term.choices)
    sim_type = serializers.ChoiceField(choices=SimType.choices)
    switch_route = serializers.ChoiceField(choices=SwitchRequest.Route.choices)
    # The number being moved (defaults to `mobile`).
    port_number = serializers.CharField(max_length=24, required=False, allow_blank=True)
    # Stored encrypted, never sent back (decision C13).
    pac_code = serializers.CharField(max_length=20, required=False, allow_blank=True)
    stac_code = serializers.CharField(max_length=20, required=False, allow_blank=True)
    promo_code = serializers.CharField(max_length=50, required=False, allow_blank=True)
    # Version of the authorisation wording shown on the page; must be current.
    consent_version = serializers.CharField(max_length=32, required=False, allow_blank=True)

    class Meta:
        model = SwitchRequest
        fields = [
            # Step 1
            "first_name",
            "last_name",
            "email",
            "mobile",
            "address_line_1",
            "address_line_2",
            "town_city",
            "postcode",
            # Step 2 (optional)
            "current_provider",
            "current_plan_cost",
            "current_data_allowance",
            # Step 3
            "plan_id",
            "contract_term",
            "sim_type",
            "promo_code",
            # Step 4
            "switch_route",
            "port_number",
            "pac_code",
            "stac_code",
            "pac_later",
            # Consent
            "transfer_authorised",
            "timeline_acknowledged",
            "terms_accepted",
            "consent_version",
            "opt_in_news",
            "opt_in_texts",
        ]

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
        cleaned = re.sub(r"[\s-]", "", value or "")
        if cleaned and not UK_MOBILE_RE.match(cleaned):
            raise serializers.ValidationError("Enter a valid UK mobile number.")
        return cleaned

    def validate_postcode(self, value):
        try:
            return services.normalise_postcode(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc))

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
                "You must authorise us to process this switching request."
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

    def validate_consent_version(self, value):
        value = value.strip()
        if value and value != consent_version():
            raise serializers.ValidationError(
                "The wording on this page has been updated. Please refresh and review it again."
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

        # The number being moved: given explicitly, else the contact number.
        number = (attrs.get("port_number") or "").strip() or attrs.get("mobile", "")
        if route == SwitchRequest.Route.KEEP_PAC and not number:
            errors["port_number"] = ["Enter the mobile number you want to bring to Zoiko."]
        elif number and route != SwitchRequest.Route.NEW_KEEP:
            try:
                attrs["port_number"] = services.normalise_msisdn(number)
            except services.PacError as exc:
                errors["port_number"] = [exc.message]
        elif route == SwitchRequest.Route.NEW_KEEP:
            attrs["port_number"] = ""  # nothing moves

        # Plan and promo, priced the same way as checkout; snapshot kept (G2).
        request = self.context.get("request")
        user = request.user if request and request.user.is_authenticated else None
        promo = (attrs.get("promo_code") or "").strip()
        attrs["promo_code"] = promo
        try:
            attrs["_quote"] = build_quote(
                user, attrs["plan_id"], attrs["contract_term"], attrs["sim_type"], promo,
                additional_line=False,
            )
        except QuoteError as exc:
            errors[_QUOTE_ERROR_FIELD.get(exc.code, "promo_code")] = [exc.message]

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def create(self, validated_data):
        from django.utils import timezone

        validated_data.pop("plan_id")
        validated_data.pop("consent_version", None)
        quote = validated_data.pop("_quote")
        code = validated_data.pop("_code", None)
        request = self.context.get("request")
        meta = request.META if request else {}
        user = request.user if request and request.user.is_authenticated else None

        instance = SwitchRequest(
            **validated_data,
            plan=quote.plan,
            selected_plan=quote.plan.name,
            quote_snapshot={
                "quote": quote.as_dict(),
                "contract_version": settings.ADD_LINE_CONTRACT_VERSION,
                "contract_summary_version": settings.ADD_LINE_CONTRACT_SUMMARY_VERSION,
            },
            quote_hash=quote.hash,
            consent_version=consent_version(),
            consented_at=timezone.now(),
            consent_ip=meta.get("REMOTE_ADDR") or None,
            consent_user_agent=(meta.get("HTTP_USER_AGENT") or "")[:255],
            user=user,
            existing_account_email=user is None and services.email_has_account(validated_data["email"]),
            idempotency_key=self.context.get("idempotency_key"),
        )
        if code:
            # Raises CodeStorageUnavailable before anything is saved.
            services.apply_code(instance, *code)
        instance.save()
        if code:
            services.record_code_received(instance)
        # Automated intake checks (M04 stage 2).
        status, reason = services.initial_status(instance)
        instance.set_status(status, reason=reason)
        return instance


# ── Other inputs ──────────────────────────────────────────────────────────────

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
