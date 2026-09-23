from rest_framework import serializers
from .models import UserSecurity, SuspiciousReport


class TwoFASerializer(serializers.Serializer):
    enabled = serializers.BooleanField()


class SuspiciousReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = SuspiciousReport
        fields = ["id", "note", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_note(self, value):
        if not value.strip():
            raise serializers.ValidationError("Please describe the suspicious activity.")
        return value
