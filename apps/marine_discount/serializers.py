from rest_framework import serializers
from .models import MarineDiscountEnrollment


class MarineDiscountEnrollmentSerializer(serializers.ModelSerializer):

    class Meta:
        model = MarineDiscountEnrollment
        fields = "__all__"
