import re
from decimal import Decimal, InvalidOperation

from rest_framework import serializers

from .models import RechargeModule, RechargeOrder, RechargeProduct


# ── Helpers ──────────────────────────────────────────────────────────────

PHONE_RE = re.compile(r"^\+?[0-9]{10,15}$")
MIN_AMOUNT = Decimal("1.00")
MAX_AMOUNT = Decimal("500.00")


def mask_identifier(value):
    """'89440123456789012' → '89**************012'"""
    if not value or len(value) < 6:
        return value or ""
    return value[:2] + "*" * (len(value) - 5) + value[-3:]


def phone_variants(phone, country_code="44"):
    """Return every format the same MSISDN might be stored in.

    +447421118918, 07421118918, 447421118918, 7421118918 all find the same SIM.
    """
    country_code = str(country_code).lstrip("+")
    cleaned = phone.strip()
    digits = re.sub(r"[^0-9]", "", cleaned)
    variants = {cleaned, digits, f"+{digits}"}

    if digits.startswith("0") and len(digits) > 10:
        intl = f"{country_code}{digits[1:]}"
        variants.update({intl, f"+{intl}"})

    if digits.startswith(country_code) and len(digits) > len(country_code):
        subscriber = digits[len(country_code):]
        variants.update({subscriber, f"0{subscriber}"})

    if len(digits) > 10:
        national = digits[-10:]
        variants.update({national, f"0{national}"})

    return [v for v in variants if v]


# ── Module ───────────────────────────────────────────────────────────────

class RechargeModuleSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()

    class Meta:
        model = RechargeModule
        fields = ["key", "name", "category", "shortcode", "enabled", "status"]

    def get_status(self, obj):
        return "Enabled" if obj.enabled else "Disabled"


# ── Product / Plan ───────────────────────────────────────────────────────

class RechargeProductSerializer(serializers.ModelSerializer):
    price = serializers.SerializerMethodField()
    formatted_price = serializers.SerializerMethodField()

    class Meta:
        model = RechargeProduct
        fields = [
            "id", "name", "slug", "module", "price_pence", "price",
            "formatted_price", "currency", "short_description",
            "rate_plan", "attributes", "is_active",
        ]

    def get_price(self, obj):
        return obj.price_pence / 100

    def get_formatted_price(self, obj):
        return obj.price_display


# ── Phone Validation ─────────────────────────────────────────────────────

class PhoneValidateSerializer(serializers.Serializer):
    phone_number = serializers.CharField(min_length=6, max_length=20)

    def validate_phone_number(self, value):
        cleaned = re.sub(r"[^0-9+]", "", value or "")
        if not PHONE_RE.match(cleaned):
            raise serializers.ValidationError("Enter a valid phone number (10–15 digits).")
        return cleaned


class SimDetailSerializer(serializers.Serializer):
    """Returned by phone validation — masked identifiers only."""
    phone_number = serializers.CharField()
    sim_card_id_masked = serializers.CharField()
    sim_iccid_masked = serializers.CharField(allow_blank=True)
    sim_status = serializers.CharField()
    rechargeable = serializers.BooleanField()
    # True only when Transatel reports the SIM as Suspended. Informational —
    # it does not block the order (see ValidatePhoneView).
    is_suspended = serializers.BooleanField(required=False, default=False)


# ── Order (read) ─────────────────────────────────────────────────────────

class RechargeOrderSerializer(serializers.ModelSerializer):
    amount = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    module_label = serializers.CharField(source="get_module_display", read_only=True)
    product_name = serializers.SerializerMethodField()

    class Meta:
        model = RechargeOrder
        fields = [
            "order_ref", "module", "module_label", "msisdn",
            "customer_name", "amount", "currency",
            "status", "status_label", "product_name", "created_at",
        ]

    def get_amount(self, obj):
        return obj.amount_display

    def get_product_name(self, obj):
        return obj.product.name if obj.product else None


# ── Order (create) ───────────────────────────────────────────────────────

class CreateRechargeSerializer(serializers.Serializer):
    """POST /api/recharge/create/

    Now accepts a product_id (plan) instead of a raw amount.
    Falls back to raw amount if product_id is not provided.
    """
    msisdn = serializers.CharField(max_length=20)
    module = serializers.ChoiceField(
        choices=[c[0] for c in RechargeModule.KEY_CHOICES], default="recharge"
    )
    product_id = serializers.IntegerField(required=False, allow_null=True)
    amount = serializers.CharField(required=False, allow_blank=True)
    sim_serial = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    sim_iccid = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    customer_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    customer_email = serializers.EmailField(required=False, allow_blank=True)
    success_url = serializers.URLField(required=False, allow_blank=True, default="")
    cancel_url = serializers.URLField(required=False, allow_blank=True, default="")

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

    def validate(self, data):
        product_id = data.get("product_id")
        raw_amount = data.get("amount")

        if product_id:
            try:
                product = RechargeProduct.objects.get(id=product_id, is_active=True)
            except RechargeProduct.DoesNotExist:
                raise serializers.ValidationError({"product_id": "Product not found or not active."})
            data["_product"] = product
            data["_amount_pence"] = product.price_pence
        elif raw_amount:
            try:
                amount = Decimal(str(raw_amount)).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                raise serializers.ValidationError({"amount": "Enter a valid amount."})
            if amount < MIN_AMOUNT or amount > MAX_AMOUNT:
                raise serializers.ValidationError(
                    {"amount": f"Amount must be between £{MIN_AMOUNT} and £{MAX_AMOUNT}."}
                )
            data["_product"] = None
            data["_amount_pence"] = int(amount * 100)
        else:
            raise serializers.ValidationError("Provide either product_id or amount.")

        return data


# ── Inline payment (matches the WordPress modal flow) ────────────────────

class CreatePaymentIntentSerializer(CreateRechargeSerializer):
    """POST /api/recharge/create-intent/

    Same inputs as CreateRechargeSerializer, but success_url / cancel_url are
    not needed because the browser never leaves the page — Stripe.js confirms
    the payment inline, exactly like the WooCommerce modal.
    """
    success_url = serializers.URLField(required=False, allow_blank=True, default="")
    cancel_url = serializers.URLField(required=False, allow_blank=True, default="")


class ConfirmPaymentSerializer(serializers.Serializer):
    """POST /api/recharge/confirm/

    Called by the browser after Stripe.js reports the payment succeeded.
    The server re-checks the PaymentIntent with Stripe before doing anything.
    """
    order_ref = serializers.CharField(max_length=24)
    payment_intent_id = serializers.CharField(max_length=255)
