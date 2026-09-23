from django.contrib import admin
from .models import Plan, PlanFeature, PlanCategory


@admin.register(PlanCategory)
class PlanCategoryAdmin(admin.ModelAdmin):
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}


class PlanFeatureInline(admin.TabularInline):
    model = PlanFeature
    extra = 1


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "transatelID",      # ✅ show in listing
        "category",
        "final_price",
        "is_active",
    )

    list_filter = ("category", "is_active")
    search_fields = ("name", "transatelID", "short_description")

    autocomplete_fields = ("category",)
    prepopulated_fields = {"slug": ("name",)}
    inlines = [PlanFeatureInline]

    tabs = (
        ("Plan Details", {
            "fieldsets": (
                ("Basic Info", {
                    "fields": (
                        "category",
                        "name",
                        "transatelID",      # ✅ added below name
                        "slug",
                        "data_allowance",
                        "tier_label",
                        "short_description",
                        "description",
                    )
                }),
                ("Pricing", {
                    "fields": (
                        "price",
                        "sale_price",
                        "price_24",
                        "price_12",
                        "price_30",
                        "duration_days",
                    )
                }),
                ("Status", {
                    "fields": ("is_popular", "is_active")
                }),
            )
        }),
        ("SEO", {
            "fieldsets": (
                ("Meta", {
                    "fields": (
                        "meta_title",
                        "meta_description",
                        "meta_keywords",
                        "canonical_url",
                    )
                }),
                ("Open Graph", {
                    "fields": (
                        "og_title",
                        "og_description",
                        "og_image",
                        "og_type",
                    )
                }),
                ("Twitter", {
                    "fields": (
                        "twitter_title",
                        "twitter_description",
                        "twitter_image",
                    )
                }),
                ("Schema", {
                    "fields": ("schema_markup",)
                }),
            )
        }),
    )

    class Media:
        js = ("plans/plan_autofill.js",)