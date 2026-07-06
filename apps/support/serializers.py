from rest_framework import serializers
from .models import SupportTicket, CallbackRequest


class SupportTicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportTicket
        fields = ["id", "subject", "category", "message", "status", "created_at"]
        read_only_fields = ["id", "status", "created_at"]

    def validate(self, attrs):
        if not attrs.get("subject", "").strip():
            raise serializers.ValidationError({"subject": "Subject is required."})
        if not attrs.get("message", "").strip():
            raise serializers.ValidationError({"message": "Message is required."})
        return attrs


class CallbackRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = CallbackRequest
        fields = ["id", "phone", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_phone(self, value):
        import re
        if not re.match(r"^\+?\d{7,15}$", value.strip()):
            raise serializers.ValidationError("Please enter a valid phone number.")
        return value
