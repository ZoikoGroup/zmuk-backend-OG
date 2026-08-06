from rest_framework import serializers

from .models import SimProduct, SimOrder


class SimProductSerializer(serializers.ModelSerializer):
    price = serializers.SerializerMethodField()

    class Meta:
        model = SimProduct
        fields = ["id", "name", "slug", "plan_name", "description", "price", "currency"]

    def get_price(self, obj):
        return obj.price_display


class SimOrderSerializer(serializers.ModelSerializer):
    amount = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = SimOrder
        fields = [
            "order_ref", "product_name", "customer_name", "email",
            "amount", "currency", "status", "status_label", "created_at",
        ]

    def get_amount(self, obj):
        return obj.amount_display


class CreateSimOrderSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(required=False)
    slug = serializers.SlugField(required=False)
    customer_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    email = serializers.EmailField()
    success_url = serializers.URLField()
    cancel_url = serializers.URLField()

    def validate(self, attrs):
        if not attrs.get("product_id") and not attrs.get("slug"):
            raise serializers.ValidationError("Provide product_id or slug.")
        qs = SimProduct.objects.filter(is_active=True)
        product = (
            qs.filter(id=attrs["product_id"]).first() if attrs.get("product_id")
            else qs.filter(slug=attrs["slug"]).first()
        )
        if product is None:
            raise serializers.ValidationError("SIM product not found or unavailable.")
        attrs["product"] = product
        return attrs
