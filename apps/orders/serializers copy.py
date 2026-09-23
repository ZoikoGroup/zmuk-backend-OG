from rest_framework import serializers
from .models import VCareOrder

class VCareOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = VCareOrder
        fields = "__all__"
