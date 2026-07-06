from django.db import models
from django.utils.text import slugify


class Category(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)

    class Meta:
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


# ─────────────────────────────────────────────────────────────────
# DYNAMIC ATTRIBUTES  (Admin creates these: "Storage", "Color" …)
# ─────────────────────────────────────────────────────────────────

class Attribute(models.Model):
    """
    Admin creates attribute types: Storage, Color, Condition, RAM …
    No code change needed to add new ones.
    """
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class AttributeValue(models.Model):
    """
    Admin creates values per attribute:
      Storage  → 128GB | 256GB | 512GB
      Color    → Black | Silver | Gold
      Condition→ New | Refurbished | Certified Unboxed
    """
    attribute = models.ForeignKey(Attribute, on_delete=models.CASCADE, related_name="values")
    value = models.CharField(max_length=100)
    slug = models.SlugField(blank=True)

    class Meta:
        unique_together = ("attribute", "value")
        ordering = ["attribute__name", "value"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.value)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.attribute.name}: {self.value}"


# ─────────────────────────────────────────────────────────────────
# PRODUCT
# ─────────────────────────────────────────────────────────────────

class Product(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, related_name="products"
    )
    description = models.TextField(blank=True)
    brand = models.CharField(max_length=100, blank=True)
    model_number = models.CharField(max_length=100, blank=True)

    # Admin picks which attributes apply to THIS product
    attributes = models.ManyToManyField(
        Attribute, blank=True, related_name="products",
        help_text="Select which attributes (Storage, Color…) apply to this product."
    )

    # Fixed spec fields
    display = models.CharField(max_length=200, blank=True)
    resolution = models.CharField(max_length=100, blank=True)
    processor = models.CharField(max_length=200, blank=True)
    ram = models.CharField(max_length=50, blank=True)
    rear_camera = models.CharField(max_length=200, blank=True)
    front_camera = models.CharField(max_length=100, blank=True)
    battery = models.CharField(max_length=100, blank=True)
    os = models.CharField(max_length=100, blank=True)
    network = models.CharField(max_length=50, blank=True)
    sim_type = models.CharField(max_length=100, blank=True)
    quick_charging = models.BooleanField(default=False)
    hybrid_sim_slot = models.BooleanField(default=False)

    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    @property
    def price_min(self):
        prices = self.variants.filter(is_active=True).values_list("price", flat=True)
        return min(prices) if prices else None

    @property
    def price_max(self):
        prices = self.variants.filter(is_active=True).values_list("price", flat=True)
        return max(prices) if prices else None

    def get_attribute_options(self):
        """
        Returns dict of available options from active variants:
        { "Storage": ["128GB", "256GB"], "Color": ["Black", "Silver"] }
        """
        result = {}
        for attr in self.attributes.all():
            values = (
                AttributeValue.objects.filter(
                    attribute=attr,
                    variantattributes__variant__product=self,
                    variantattributes__variant__is_active=True,
                )
                .values_list("value", flat=True)
                .distinct()
            )
            result[attr.name] = list(values)
        return result


class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="products/")
    alt_text = models.CharField(max_length=200, blank=True)
    is_primary = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.product.name} – Image {self.order}"


# ─────────────────────────────────────────────────────────────────
# VARIANT  (one specific combination of attribute values)
# ─────────────────────────────────────────────────────────────────

class ProductVariant(models.Model):
    """
    Represents one purchasable combination.
    e.g. Galaxy S21 Ultra → Storage=256GB + Color=Black + Condition=Refurbished
    Admin creates as many variants as needed. No code changes required.
    """
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")
    sku = models.CharField(max_length=100, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    stock = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    image = models.ForeignKey(
        ProductImage, on_delete=models.SET_NULL, null=True, blank=True,
        help_text="Optional: variant-specific image (e.g. color swatch photo)"
    )

    class Meta:
        ordering = ["price"]

    def __str__(self):
        attrs = self.variant_attributes.select_related(
            "attribute_value__attribute"
        ).all()
        combo = " / ".join(str(a.attribute_value.value) for a in attrs)
        return f"{self.product.name} [{combo}]" if combo else self.product.name

    def get_attributes_dict(self):
        """Returns { "Storage": "256GB", "Color": "Black", "Condition": "Refurbished" }"""
        return {
            va.attribute_value.attribute.name: va.attribute_value.value
            for va in self.variant_attributes.select_related(
                "attribute_value__attribute"
            ).all()
        }


class VariantAttribute(models.Model):
    """
    Through-table: assigns attribute values to a variant.
    e.g.  Variant #5 → Storage=256GB
          Variant #5 → Color=Black
    """
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.CASCADE, related_name="variant_attributes"
    )
    attribute_value = models.ForeignKey(
        AttributeValue, on_delete=models.CASCADE, related_name="variantattributes"
    )

    class Meta:
        unique_together = ("variant", "attribute_value")

    def __str__(self):
        return f"{self.variant.sku} → {self.attribute_value}"
