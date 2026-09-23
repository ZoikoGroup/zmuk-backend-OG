from rest_framework import serializers
from .models import SeniorCitizenDiscount

class SeniorCitizenDiscountSerializer(serializers.ModelSerializer):
    class Meta:
        model = SeniorCitizenDiscount
        fields = "__all__"
