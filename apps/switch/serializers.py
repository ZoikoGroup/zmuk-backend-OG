import re

from rest_framework import serializers

from .models import SwitchRequest

# UK mobile: 07xxxxxxxxx or +447xxxxxxxxx
UK_MOBILE_RE = re.compile(r"^(?:\+44|0)7\d{9}$")


class SwitchRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = SwitchRequest
        fields = [
            "id",
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
            "selected_plan",
            # Authorisation
            "transfer_authorised",
            "timeline_acknowledged",
            "terms_accepted",
            # Read-only
            "status",
            "pac_code",
            "created_at",
        ]
        read_only_fields = ["id", "status", "pac_code", "created_at"]

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
