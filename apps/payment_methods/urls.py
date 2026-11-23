from django.urls import path

from .views import (
    PaymentMethodListView,
    RemoveView,
    SetDefaultView,
    SetupConfirmView,
    SetupIntentView,
    stripe_webhook,
)

urlpatterns = [
    path("", PaymentMethodListView.as_view(), name="payment_methods_list"),
    path("setup-intent/", SetupIntentView.as_view(), name="payment_methods_setup_intent"),
    path("setup-intent/confirm/", SetupConfirmView.as_view(), name="payment_methods_setup_confirm"),
    path("<uuid:method_id>/default/", SetDefaultView.as_view(), name="payment_methods_set_default"),
    path("<uuid:method_id>/remove/", RemoveView.as_view(), name="payment_methods_remove"),
    path("webhook/", stripe_webhook, name="payment_methods_webhook"),
]
