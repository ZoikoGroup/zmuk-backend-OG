from rest_framework import serializers

from .models import TravelEcosystemPartnerInquiry


class TravelEcosystemPartnerInquirySerializer(
    serializers.ModelSerializer
):

    class Meta:
        model = TravelEcosystemPartnerInquiry

        fields = "__all__"