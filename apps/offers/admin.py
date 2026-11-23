from django import forms
from django.contrib import admin

from .models import MAX_BULLETS, Offer
from .services import is_live


class OfferAdminForm(forms.ModelForm):
    """Bullets are typed one per line instead of as JSON."""

    bullets = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": MAX_BULLETS}),
        help_text=f"One point per line, up to {MAX_BULLETS}.",
    )

    class Meta:
        model = Offer
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if isinstance(self.initial.get("bullets"), list):
            self.initial["bullets"] = "\n".join(self.initial["bullets"])

    def clean_bullets(self):
        text = self.cleaned_data.get("bullets") or ""
        return [line.strip() for line in text.splitlines() if line.strip()]


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    form = OfferAdminForm

    list_display = (
        "title",
        "mechanism",
        "coupon",
        "starts_at",
        "ends_at",
        "priority",
        "is_published",
        "shown_now",
    )
    list_editable = ("priority", "is_published")
    list_filter = ("is_published", "mechanism")
    search_fields = ("title", "slug", "coupon__name", "coupon__slug")
    prepopulated_fields = {"slug": ("title",)}
    autocomplete_fields = ("coupon",)
    readonly_fields = ("created_at", "updated_at", "updated_by")
    ordering = ("priority", "ends_at")

    fieldsets = (
        ("Card", {"fields": ("title", "slug", "subtitle", "badge", "bullets", "terms_text")}),
        ("Discount", {
            "fields": ("mechanism", "coupon"),
            "description": "A coupon offer shows the coupon's code and discount. "
                           "Checkout checks the code as usual.",
        }),
        ("When", {"fields": ("starts_at", "ends_at")}),
        ("Button", {"fields": ("cta_label", "cta_href")}),
        ("Publishing", {"fields": ("priority", "is_published")}),
        ("History", {"fields": ("created_at", "updated_at", "updated_by"), "classes": ("collapse",)}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("coupon").prefetch_related("coupon__users")

    @admin.display(description="Shown now", boolean=True)
    def shown_now(self, obj):
        # Same rule as the API: published, inside its times, coupon still works.
        return is_live(obj)

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
