from rest_framework import serializers
from .models import (
    Category, Attribute, AttributeValue,
    Product, ProductImage, ProductVariant, VariantAttribute,
)


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "slug"]


class AttributeValueSerializer(serializers.ModelSerializer):
    attribute_name = serializers.CharField(source="attribute.name", read_only=True)

    class Meta:
        model = AttributeValue
        fields = ["id", "attribute_name", "value", "slug"]


class AttributeSerializer(serializers.ModelSerializer):
    values = AttributeValueSerializer(many=True, read_only=True)

    class Meta:
        model = Attribute
        fields = ["id", "name", "slug", "values"]


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ["id", "image", "alt_text", "is_primary", "order"]


class VariantAttributeSerializer(serializers.ModelSerializer):
    attribute_name = serializers.CharField(
        source="attribute_value.attribute.name", read_only=True
    )
    attribute_slug = serializers.CharField(
        source="attribute_value.attribute.slug", read_only=True
    )
    value = serializers.CharField(source="attribute_value.value", read_only=True)
    value_slug = serializers.CharField(source="attribute_value.slug", read_only=True)

    class Meta:
        model = VariantAttribute
        fields = ["attribute_name", "attribute_slug", "value", "value_slug"]


class ProductVariantSerializer(serializers.ModelSerializer):
    attributes = VariantAttributeSerializer(
        source="variant_attributes", many=True, read_only=True
    )
    attributes_dict = serializers.SerializerMethodField()
    in_stock = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = [
            "id", "sku", "price", "stock", "in_stock",
            "is_active", "image", "attributes", "attributes_dict",
        ]

    def get_attributes_dict(self, obj):
        return obj.get_attributes_dict()

    def get_in_stock(self, obj):
        return obj.stock > 0


# ── List serializer (lightweight) ─────────────────────────────────────────

class ProductListSerializer(serializers.ModelSerializer):
    primary_image = serializers.SerializerMethodField()
    category = CategorySerializer(read_only=True)
    price_min = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    price_max = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    # 👇 ADD THIS LINE
    attributes = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id", "name", "slug", "category", "brand",
            "price_min", "price_max", "primary_image", "is_featured",
            "attributes",  # 👈 ADD THIS
        ]

    def get_primary_image(self, obj):
        img = obj.images.filter(is_primary=True).first() or obj.images.first()
        if img:
            request = self.context.get("request")
            return request.build_absolute_uri(img.image.url) if request else img.image.url
        return None

    # 👇 ADD THIS FUNCTION HERE (INSIDE SAME CLASS)
    def get_attributes(self, obj):
        attr_map = {}

        for variant in obj.variants.all():
            for va in variant.variant_attributes.all():
                name = va.attribute_value.attribute.name
                value = va.attribute_value.value

                if name not in attr_map:
                    attr_map[name] = set()

                attr_map[name].add(value)

        return [
            {"name": k, "values": list(v)}
            for k, v in attr_map.items()
        ]
    primary_image = serializers.SerializerMethodField()
    category = CategorySerializer(read_only=True)
    price_min = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    price_max = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    attributes = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id", "name", "slug", "category", "brand",
            "price_min", "price_max", "primary_image", "is_featured",
            "attributes",
        ]

    def get_primary_image(self, obj):
        img = obj.images.filter(is_primary=True).first() or obj.images.first()
        if img:
            request = self.context.get("request")
            return request.build_absolute_uri(img.image.url) if request else img.image.url
        return None


# ── Detail serializer (full) ───────────────────────────────────────────────

class ProductDetailSerializer(serializers.ModelSerializer):
    images = ProductImageSerializer(many=True, read_only=True)
    variants = ProductVariantSerializer(many=True, read_only=True)
    category = CategorySerializer(read_only=True)
    price_min = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    price_max = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    # Dynamic: each attribute with its available option values
    attribute_options = serializers.SerializerMethodField()

    # The actual attribute definitions used by this product
    attributes = AttributeSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = [
            "id", "name", "slug", "category", "description",
            "price_min", "price_max", "brand", "model_number",
            "display", "resolution", "processor", "ram",
            "rear_camera", "front_camera", "battery", "os",
            "network", "sim_type", "quick_charging", "hybrid_sim_slot",
            "images", "variants", "attributes", "attribute_options",
            "is_featured", "created_at",
        ]

    def get_attribute_options(self, obj):
        """
        Returns:
        [
          { "name": "Storage", "slug": "storage", "options": ["128GB", "256GB"] },
          { "name": "Color",   "slug": "color",   "options": ["Black", "Silver"] },
        ]
        Frontend uses this to render the dropdowns dynamically.
        """
        result = []
        for attr in obj.attributes.all():
            values = (
                AttributeValue.objects.filter(
                    attribute=attr,
                    variantattributes__variant__product=obj,
                    variantattributes__variant__is_active=True,
                )
                .values_list("value", flat=True)
                .distinct()
            )
            result.append({
                "name": attr.name,
                "slug": attr.slug,
                "options": list(values),
            })
        return result
    
class ProductSerializer(serializers.ModelSerializer):
    variants = ProductVariantSerializer(many=True, read_only=True)
    attributes = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id',
            'name',
            'slug',
            'category',
            'brand',
            'price_min',
            'price_max',
            'primary_image',
            'is_featured',
            'attributes',   # 👈 NEW
            'variants'      # 👈 NEW
        ]
    def get_attributes(self, obj):
        attr_map = {}

        for variant in obj.variants.all():
            for va in variant.variant_attributes.all():
                name = va.attribute_value.attribute.name
                value = va.attribute_value.value

                if name not in attr_map:
                    attr_map[name] = set()

                attr_map[name].add(value)

        return [
            {"name": k, "values": list(v)}
            for k, v in attr_map.items()
        ]
