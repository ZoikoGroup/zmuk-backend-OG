from rest_framework import serializers
from .models import EnterpriseInquiry


class EnterpriseInquirySerializer(serializers.ModelSerializer):

    class Meta:
        model = EnterpriseInquiry
        fields = "__all__"