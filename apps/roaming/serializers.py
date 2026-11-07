from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .models import Destination

# Back-dated quotes are allowed so a customer can check a recent trip; the
# register's effective dates decide which tariff applied (CT-001 §09-5).
MAX_PAST_DAYS = 90
MAX_FUTURE_DAYS = 365


class TravelDateSerializer(serializers.Serializer):
    travel_date = serializers.DateField(required=False)

    def validate_travel_date(self, value):
        today = timezone.localdate()
        if value < today - timedelta(days=MAX_PAST_DAYS):
            raise serializers.ValidationError(f"Travel date can't be more than {MAX_PAST_DAYS} days ago.")
        if value > today + timedelta(days=MAX_FUTURE_DAYS):
            raise serializers.ValidationError(f"Travel date can't be more than {MAX_FUTURE_DAYS} days ahead.")
        return value

    def validated_travel_date(self):
        return self.validated_data.get("travel_date") or timezone.localdate()


class LineQuoteParamsSerializer(TravelDateSerializer):
    country = serializers.CharField(max_length=16)


class DestinationSerializer(serializers.ModelSerializer):
    rating_zone = serializers.SerializerMethodField()

    class Meta:
        model = Destination
        fields = ("code", "name", "kind", "parent_code", "rating_zone")

    def get_rating_zone(self, destination):
        return self.context.get("zones", {}).get(destination.pk)
