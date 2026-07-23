from decimal import Decimal, InvalidOperation

from rest_framework import serializers

from .models import RechargeModule, RechargeOrder

# Server-side money guardrails (never trust the client's amount blindly).
MIN_AMOUNT = Decimal("1.00")
MAX_AMOUNT = Decimal("100.00")


class RechargeModuleSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()

    class Meta:
        model = RechargeModule
        fields = ["key", "name", "category", "shortcode", "enabled", "status"]

    def get_status(self, obj):
        return "Enabled" if obj.enabled else "Disabled"


class RechargeOrderSerializer(serializers.ModelSerializer):
    amount = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    module_label = serializers.CharField(source="get_module_display", read_only=True)

    class Meta:
        model = RechargeOrder
        fields = [
            "order_ref", "module", "module_label", "msisdn",
            "customer_name", "amount", "currency",
            "status", "status_label", "created_at",
        ]

    def get_amount(self, obj):
        return obj.amount_display


class CreateRechargeSerializer(serializers.Serializer):
    msisdn = serializers.CharField(max_length=20)
    module = serializers.ChoiceField(choices=[c[0] for c in RechargeModule.KEY_CHOICES], default="recharge")
    amount = serializers.CharField()  # pounds, e.g. "5.66" — validated -> pence
    customer_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    customer_email = serializers.EmailField(required=False, allow_blank=True)
    success_url = serializers.URLField()
    cancel_url = serializers.URLField()

    def validate_msisdn(self, value):
        digits = "".join(ch for ch in value if ch.isdigit())
        if len(digits) < 7:
            raise serializers.ValidationError("Enter a valid phone number.")
        return value

    def validate_module(self, value):
        mod = RechargeModule.objects.filter(key=value).first()
        if mod is None or not mod.enabled:
            raise serializers.ValidationError("This recharge module is not available.")
        return value

    def validate_amount(self, value):
        try:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            raise serializers.ValidationError("Enter a valid amount.")
        if amount < MIN_AMOUNT or amount > MAX_AMOUNT:
            raise serializers.ValidationError(f"Amount must be between £{MIN_AMOUNT} and £{MAX_AMOUNT}.")
        # stash the pence value for the view
        self.context["amount_pence"] = int(amount * 100)
        return value
