from rest_framework import serializers
from .models import RequestDemo

class RequestDemoSerializer(serializers.ModelSerializer):
    class Meta:
        model = RequestDemo
        fields = '__all__'