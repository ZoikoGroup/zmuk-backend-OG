from rest_framework import serializers
from .models import IntegrationInterest


class IntegrationInterestSerializer(serializers.ModelSerializer):

    class Meta:
        model = IntegrationInterest
        fields = "__all__"