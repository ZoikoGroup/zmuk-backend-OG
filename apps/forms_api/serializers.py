from rest_framework import serializers
from .models import RequestForm

class RequestFormSerializer(serializers.ModelSerializer):
    class Meta:
        model = RequestForm
        fields = '__all__'