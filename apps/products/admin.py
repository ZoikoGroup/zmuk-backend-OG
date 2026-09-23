from django.contrib import admin
from django.utils.html import format_html
from .models import (
    Category, Attribute, AttributeValue,
    Product, ProductImage, ProductVariant, VariantAttribute,
)


# ── Attribute & Values ─────────────────────────────────────────────────────

class AttributeValueInline(admin.TabularInline):
    model = AttributeValue
    extra = 2
    fields = ["value", "slug"]
    prepopulated_fields = {"slug": ("value",)}


@admin.register(Attribute)
class AttributeAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "value_count"]
    prepopulated_fields = {"slug": ("name",)}
    inlines = [AttributeValueInline]

    def value_count(self, obj):
        return obj.values.count()
    value_count.short_description = "# Values"


@admin.register(AttributeValue)
class AttributeValueAdmin(admin.ModelAdmin):
    list_display = ["attribute", "value", "slug"]
    list_filter = ["attribute"]
    prepopulated_fields = {"slug": ("value",)}


# ── Category ───────────────────────────────────────────────────────────────

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}


# ── Product ────────────────────────────────────────────────────────────────

class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1
    fields = ["image", "alt_text", "is_primary", "order"]
    readonly_fields = ["preview"]

    def preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="height:50px;border-radius:4px"/>', obj.image.url)
        return "—"


class VariantAttributeInline(admin.TabularInline):
    model = VariantAttribute
    extra = 1
    fields = ["attribute_value"]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # Limit attribute values to those belonging to the parent product's attributes
        if db_field.name == "attribute_value":
            # Get the product from the parent object
            parent_id = request.resolver_match.kwargs.get("object_id")
            if parent_id:
                try:
                    variant = ProductVariant.objects.get(pk=parent_id)
                    kwargs["queryset"] = AttributeValue.objects.filter(
                        attribute__in=variant.product.attributes.all()
                    ).select_related("attribute")
                except ProductVariant.DoesNotExist:
                    pass
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class ProductVariantInline(admin.StackedInline):
    model = ProductVariant
    extra = 0
    fields = ["sku", "price", "stock", "is_active", "image"]
    show_change_link = True  # allows drilling into variant to add VariantAttributes


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ["__str__", "product", "sku", "price", "stock", "is_active", "attribute_summary"]
    list_filter = ["is_active", "product__brand"]
    search_fields = ["sku", "product__name"]
    inlines = [VariantAttributeInline]

    def attribute_summary(self, obj):
        return " / ".join(
            f"{va.attribute_value.attribute.name}={va.attribute_value.value}"
            for va in obj.variant_attributes.select_related("attribute_value__attribute").all()
        )
    attribute_summary.short_description = "Attributes"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["name", "brand", "category", "price_min", "price_max", "variant_count", "is_active", "is_featured"]
    list_filter = ["is_active", "is_featured", "brand", "category"]
    search_fields = ["name", "brand", "model_number"]
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ["attributes"]  # nice widget for selecting attributes
    inlines = [ProductImageInline, ProductVariantInline]

    fieldsets = (
        ("Basic Info", {
            "fields": (
                "name", "slug", "category", "brand", "model_number",
                "description", "is_active", "is_featured",
            )
        }),
        ("Variable Product Attributes", {
            "fields": ("attributes",),
            "description": (
                "Select which attributes apply to this product (e.g. Storage, Color, Condition). "
                "Then add variants below with specific combinations."
            ),
        }),
        ("Specifications", {
            "fields": (
                "display", "resolution", "processor", "ram",
                "rear_camera", "front_camera", "battery", "os",
                "network", "sim_type", "quick_charging", "hybrid_sim_slot",
            ),
            "classes": ("collapse",),
        }),
    )

    def price_min(self, obj):
        p = obj.price_min
        return f"${p}" if p else "—"
    price_min.short_description = "From"

    def price_max(self, obj):
        p = obj.price_max
        return f"${p}" if p else "—"
    price_max.short_description = "To"

    def variant_count(self, obj):
        return obj.variants.count()
    variant_count.short_description = "Variants"
