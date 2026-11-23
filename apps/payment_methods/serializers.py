from django.utils import timezone
from rest_framework import serializers

from .models import PaymentMethod


class PaymentMethodSerializer(serializers.ModelSerializer):
    """Display details only: never the fingerprint or the Stripe ID."""

    type = serializers.CharField(source="method_type")
    expired = serializers.SerializerMethodField()

    class Meta:
        model = PaymentMethod
        fields = ["id", "type", "brand", "last4", "exp_month", "exp_year", "expired", "is_default", "created_at"]

    def get_expired(self, obj) -> bool:
        if not obj.exp_month or not obj.exp_year:
            return False
        today = timezone.localdate()
        return (obj.exp_year, obj.exp_month) < (today.year, today.month)


class SetupIntentRequestSerializer(serializers.Serializer):
    consent = serializers.BooleanField()
    consent_version = serializers.CharField(max_length=40)

    def validate_consent(self, value):
        if not value:
            raise serializers.ValidationError("Tick the box to save your card.")
        return value


class SetupConfirmSerializer(serializers.Serializer):
    setup_intent_id = serializers.CharField(max_length=255)


class PasswordSerializer(serializers.Serializer):
    # Optional: social-login accounts have no password (recent login instead).
    password = serializers.CharField(required=False, allow_blank=True, default="", trim_whitespace=False)
