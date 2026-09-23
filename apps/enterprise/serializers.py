from rest_framework import serializers
from .models import EnterpriseInquiry


class EnterpriseInquirySerializer(
    serializers.ModelSerializer
):

    class Meta:
        model = EnterpriseInquiry
        fields = "__all__"

    def validate_consent(self, value):

        if value is not True:

            raise serializers.ValidationError(
                "Consent is required."
            )

        return value