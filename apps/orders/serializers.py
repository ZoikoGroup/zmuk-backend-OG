from rest_framework import serializers
from .models import BqOrder
import json


class BqOrderSerializer(serializers.ModelSerializer):
    parsed_raw_data = serializers.SerializerMethodField()

    class Meta:
        model = BqOrder
        fields = "__all__"

    def get_parsed_raw_data(self, obj):
        try:
            return json.loads(obj.raw_data)
        except Exception:
            return {}
