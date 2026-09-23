from datetime import date
import re

from rest_framework import serializers

from .models import StudentDiscountApplication


class StudentDiscountApplicationSerializer(serializers.ModelSerializer):

    class Meta:
        model = StudentDiscountApplication
        fields = "__all__"

    def validate_mobile(self, value):
        pattern = r"^(\+44|0)7\d{9}$"

        if not re.match(pattern, value):
            raise serializers.ValidationError(
                "Please enter a valid UK mobile number."
            )

        return value

    def validate_dob(self, value):
        today = date.today()

        age = (
            today.year
            - value.year
            - (
                (today.month, today.day)
                < (value.month, value.day)
            )
        )

        if age < 16:
            raise serializers.ValidationError(
                "Applicant must be at least 16 years old."
            )

        return value

    def validate_graduation_date(self, value):
        if value < date.today():
            raise serializers.ValidationError(
                "Graduation date cannot be in the past."
            )

        return value

    def validate_student_id_document(self, value):

        allowed = [
            "image/jpeg",
            "image/png",
            "application/pdf",
        ]

        if value.content_type not in allowed:
            raise serializers.ValidationError(
                "Only JPG, PNG and PDF files are allowed."
            )

        if value.size > 5 * 1024 * 1024:
            raise serializers.ValidationError(
                "Maximum file size is 5MB."
            )

        return value