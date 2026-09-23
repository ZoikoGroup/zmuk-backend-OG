from rest_framework import serializers
from .models import TravelPartner


class TravelPartnerSerializer(serializers.ModelSerializer):

    class Meta:
        model = TravelPartner
        fields = "__all__"

    def validate_business_email(self, value):

        if not value:
            raise serializers.ValidationError(
                "Business email is required."
            )

        return value

    def validate_consent(self, value):

        if value is not True:
            raise serializers.ValidationError(
                "Consent is required."
            )

        return value