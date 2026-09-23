from rest_framework import serializers
from .models import MilitaryDiscountEnrollment
from datetime import date


class MilitaryDiscountEnrollmentSerializer(serializers.ModelSerializer):

    class Meta:
        model = MilitaryDiscountEnrollment
        fields = "__all__"

    def validate_dob(self, value):
        today = date.today()
        age = today.year - value.year - (
            (today.month, today.day) < (value.month, value.day)
        )
        if age < 18:
            raise serializers.ValidationError(
                "You must be at least 18 years old."
            )
        return value
