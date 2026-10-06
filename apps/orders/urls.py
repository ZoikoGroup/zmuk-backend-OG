from django.urls import path
from .views import (
    BqOrderCreateAPIView,
    BqUserGroupedOrdersAPIView,
    CheckoutCreateIntentView,
    CheckoutOrderStatusView,
    ConfirmCheckoutView,
    checkout_webhook,
)

urlpatterns = [
    path("bqorders/", BqOrderCreateAPIView.as_view(), name="bqorders_create"),
    path("bqorders/by-user/", BqUserGroupedOrdersAPIView.as_view(), name="bqorders_by_user"),
    path("checkout/create-intent/", CheckoutCreateIntentView.as_view(), name="checkout_create_intent"),
    path("checkout/order-status/<str:order_ref>/", CheckoutOrderStatusView.as_view(), name="checkout_order_status"),
    path("checkout/confirm/", ConfirmCheckoutView.as_view(), name="checkout_confirm"),
    path("checkout/webhook/", checkout_webhook, name="checkout_webhook"),
]
