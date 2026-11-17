from datetime import date
import re

from rest_framework import serializers

from .models import StudentDiscountApplication
from .services import public_status


class StudentDiscountApplicationSerializer(serializers.ModelSerializer):

    class Meta:
        model = StudentDiscountApplication
        # Listed explicitly so status/user/review fields can't be set by the client.
        fields = [
            "id",
            "full_name",
            "dob",
            "email",
            "mobile",
            "institution",
            "student_id_number",
            "enrolment_status",
            "graduation_date",
            "selected_plan",
            "contract_duration",
            "roaming",
            "wifi_calling",
            "esim",
            "student_id_document",
            "signature",
            "declaration_date",
            "status",
            "created_at",
        ]
        read_only_fields = ["id", "status", "created_at"]
        # The document is private: accepted on upload, never returned.
        extra_kwargs = {"student_id_document": {"write_only": True}}

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["status"] = public_status(instance.status)
        return data

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
