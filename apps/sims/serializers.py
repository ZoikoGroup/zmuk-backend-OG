"""Serializers for the SIM ordering API."""

from rest_framework import serializers

from .models import Sim


class SimSerializer(serializers.ModelSerializer):
    sim_type = serializers.CharField(source="sim_type_key", read_only=True)

    class Meta:
        model = Sim
        fields = [
            "id", "iccid", "msisdn", "type_of_sim", "sim_type",
            "provisioning_status", "inventory", "order_reference",
            "activated_at", "activation_transaction_id",
        ]
        read_only_fields = fields


class OrderItemSerializer(serializers.Serializer):
    """One checkout line — a SIM plan."""
    id = serializers.CharField(required=False, allow_blank=True)
    cartKey = serializers.CharField(required=False, allow_blank=True)
    name = serializers.CharField(required=False, allow_blank=True)
    quantity = serializers.IntegerField(min_value=1, default=1)
    simType = serializers.ChoiceField(choices=["esim", "psim"])
    transatelID = serializers.CharField()
    duration = serializers.IntegerField(required=False, allow_null=True)
    dataAllowance = serializers.CharField(required=False, allow_blank=True)


class SimOrderSerializer(serializers.Serializer):
    """Payload the checkout posts to place a SIM order."""
    email = serializers.EmailField(required=False, allow_blank=True)
    user_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    order_reference = serializers.CharField(required=False, allow_blank=True)
    country_code = serializers.CharField(required=False, allow_blank=True)
    items = OrderItemSerializer(many=True)


class ReserveSerializer(serializers.Serializer):
    simType = serializers.ChoiceField(choices=["esim", "psim"])
    quantity = serializers.IntegerField(min_value=1, default=1)
    cart_key = serializers.CharField()
    session_id = serializers.CharField()


class ReleaseSerializer(serializers.Serializer):
    cart_key = serializers.CharField()
