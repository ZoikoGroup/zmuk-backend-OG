from django.db import models
from django.utils.text import slugify


class PlanCategory(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True, blank=True)

    class Meta:
        verbose_name_plural = "Plan Categories"
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            counter = 1
            while PlanCategory.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Plan(models.Model):

    category = models.ForeignKey(
        PlanCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="plans",
    )

    name = models.CharField(max_length=150)

    transatelID = models.CharField(
        max_length=100,
    
        blank=True,
        null=True,
    )

    slug = models.SlugField(unique=True, blank=True)

    short_description = models.TextField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)

    price = models.DecimalField(max_digits=8, decimal_places=2)
    sale_price = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        blank=True,
        null=True,
    )

    price_24 = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        blank=True,
        null=True,
    )
    price_12 = models.DecimalField(   # 12 Month Plan price
        max_digits=8,
        decimal_places=2,
        blank=True,
        null=True,
    )
    price_30 = models.DecimalField(   # 30 Day Plan price
        max_digits=8,
        decimal_places=2,
        blank=True,
        null=True,
    )

    # Card display fields
    data_allowance = models.CharField(  # e.g. "30GB", "100GB", "UNLIMITED"
        max_length=50,
        blank=True,
        null=True,
    )
    tier_label = models.CharField(      # e.g. "ESSENTIAL", "PROFESSIONAL", "ENTERPRISE"
        max_length=50,
        blank=True,
        null=True,
    )

    duration_days = models.PositiveIntegerField()

    # SEO
    meta_title = models.CharField(max_length=255, blank=True, null=True)
    meta_description = models.TextField(blank=True, null=True)
    meta_keywords = models.CharField(max_length=255, blank=True, null=True)
    canonical_url = models.URLField(blank=True, null=True)

    og_title = models.CharField(max_length=255, blank=True, null=True)
    og_description = models.TextField(blank=True, null=True)
    og_image = models.ImageField(upload_to="seo/og/", blank=True, null=True)
    og_type = models.CharField(max_length=50, blank=True, null=True)

    twitter_title = models.CharField(max_length=255, blank=True, null=True)
    twitter_description = models.TextField(blank=True, null=True)
    twitter_image = models.ImageField(upload_to="seo/twitter/", blank=True, null=True)

    schema_markup = models.JSONField(blank=True, null=True)

    is_popular = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        # Slug generation
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            counter = 1
            while Plan.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug

        # SEO fallback (SAFE)
        self.meta_title = self.meta_title or self.name
        self.meta_description = (
            self.meta_description
            or self.short_description
            or self.name
        )
        self.og_title = self.og_title or self.meta_title
        self.og_description = self.og_description or self.meta_description
        self.twitter_title = self.twitter_title or self.meta_title
        self.twitter_description = self.twitter_description or self.meta_description

        super().save(*args, **kwargs)

    @property
    def final_price(self):
        return self.sale_price if self.sale_price else self.price

    def __str__(self):
        return self.name


class PlanFeature(models.Model):
    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="features",
    )
    title = models.CharField(max_length=255)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.title