from rest_framework import serializers
from .models import FirstResponderApplication

class FirstResponderSerializer(serializers.ModelSerializer):
    class Meta:
        model = FirstResponderApplication
        fields = "__all__"
