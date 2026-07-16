from datetime import date

from rest_framework import serializers

from .models import SimActivation


class OtpRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class OtpVerifySerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.CharField()

    def validate_otp(self, value):
        value = value.strip()
        if not value.isdigit() or len(value) != 6:
            raise serializers.ValidationError("OTP must be exactly 6 digits.")
        return value


class SimActivationSerializer(serializers.ModelSerializer):

    simSerial = serializers.CharField(source="sim_serial")
    firstName = serializers.CharField(source="first_name")
    lastName = serializers.CharField(source="last_name")
    zoikoPackage = serializers.CharField(source="zoiko_package")

    class Meta:
        model = SimActivation

        fields = [
            "username",
            "email",
            "otp",
            "simSerial",
            "title",
            "firstName",
            "lastName",
            "dob",
            "zoikoPackage",
            "country",
            "postcode",
            "city",
            "address",
        ]

    def validate_otp(self, value):
        value = value.strip()
        if not value.isdigit() or len(value) != 6:
            raise serializers.ValidationError("OTP must be exactly 6 digits.")
        return value

    def validate_sim_serial(self, value):
        value = value.replace(" ", "")

        if not value.isdigit():
            raise serializers.ValidationError(
                "SIM Serial Number must contain only digits."
            )

        if len(value) < 18 or len(value) > 22:
            raise serializers.ValidationError(
                "SIM Serial Number must be between 18 and 22 digits."
            )

        return value

    def validate_dob(self, value):
        today = date.today()

        age = (
            today.year
            - value.year
            - ((today.month, today.day) < (value.month, value.day))
        )

        if age < 18:
            raise serializers.ValidationError(
                "You must be at least 18 years old."
            )

        return value