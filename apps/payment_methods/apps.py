from django.apps import AppConfig


class PaymentMethodsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.payment_methods"
    verbose_name = "Saved payment methods"
