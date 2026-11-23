from django.contrib import admin

from .models import PaymentMethod, PaymentMethodConsent, PaymentMethodSetup, StripeCustomer


class ReadOnlyAdmin(admin.ModelAdmin):
    """Support can look, not edit: changes go through Stripe and the API."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StripeCustomer)
class StripeCustomerAdmin(ReadOnlyAdmin):
    list_display = ["user", "stripe_customer_id", "livemode", "created_at"]
    search_fields = ["user__email", "stripe_customer_id"]
    list_filter = ["livemode"]


@admin.register(PaymentMethod)
class PaymentMethodAdmin(ReadOnlyAdmin):
    list_display = ["user", "brand", "last4", "exp_month", "exp_year", "is_default", "status", "created_at"]
    search_fields = ["user__email", "last4"]
    list_filter = ["status", "brand", "allowed_usage"]
    exclude = ["fingerprint"]


@admin.register(PaymentMethodConsent)
class PaymentMethodConsentAdmin(ReadOnlyAdmin):
    list_display = ["user", "kind", "wording_version", "accepted_at", "revoked_at"]
    search_fields = ["user__email"]
    list_filter = ["kind", "wording_version"]


@admin.register(PaymentMethodSetup)
class PaymentMethodSetupAdmin(ReadOnlyAdmin):
    list_display = ["user", "source", "status", "created_at"]
    search_fields = ["user__email", "stripe_intent_id"]
    list_filter = ["source", "status"]
