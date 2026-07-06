from rest_framework import serializers
from .models import Plan, PlanFeature, PlanCategory


class PlanFeatureSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanFeature
        fields = ("id", "title")


class PlanCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanCategory
        fields = ("id", "name", "slug")


class PlanSerializer(serializers.ModelSerializer):
    features = PlanFeatureSerializer(many=True, read_only=True)
    category = PlanCategorySerializer(read_only=True)
    final_price = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = (
            "id",
            "name",
            "transatelID",
            "slug",
            "short_description",
            "description",

            "price",
            "sale_price",
            "price_24",
            "price_12",
            "price_30",
            "data_allowance",
            "tier_label",
            "final_price",
            "duration_days",

            # SEO
            "meta_title",
            "meta_description",
            "meta_keywords",
            "canonical_url",
            "og_title",
            "og_description",
            "og_image",
            "og_type",
            "twitter_title",
            "twitter_description",
            "twitter_image",
            "schema_markup",

            "is_popular",
            "is_active",
            "created_at",

            "category",
            "features",
        )

    def get_final_price(self, obj):
        return obj.sale_price or obj.price