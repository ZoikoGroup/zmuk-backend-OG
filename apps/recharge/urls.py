from django.urls import path

from .views import (
    ValidatePhoneView,
    RechargeProductsView,
    RechargeModulesView,
    RechargeStatsView,
    RechargeOrdersView,
    RechargeOrderDetailView,
    RechargeOrderStatusView,
    CreateRechargeView,
    CreatePaymentIntentView,
    ConfirmPaymentView,
    TransatelLogsView,
    stripe_webhook,
)
app_name = "recharge"
urlpatterns = [
    # Phone validation (Step 1)
    path("validate-phone/", ValidatePhoneView.as_view(), name="recharge_validate_phone"),

    # Product catalog (Step 2)
    path("products/", RechargeProductsView.as_view(), name="recharge_products"),

    # Order creation + payment (Steps 3-4)
    #
    # Two payment modes:
    #   create/         -> Stripe Checkout, redirects away to Stripe's page
    #   create-intent/  -> Stripe PaymentIntent, pays INLINE on your own page
    #                      (this is the one that matches the WordPress modal)
    path("create/", CreateRechargeView.as_view(), name="recharge_create"),
    path("create-intent/", CreatePaymentIntentView.as_view(), name="recharge_create_intent"),
    path("confirm/", ConfirmPaymentView.as_view(), name="recharge_confirm"),

    # Order lookup
    path("orders/", RechargeOrdersView.as_view(), name="recharge_orders"),
    path("orders/<str:order_ref>/", RechargeOrderDetailView.as_view(), name="recharge_order_detail"),
    path("order-status/<str:order_ref>/", RechargeOrderStatusView.as_view(), name="recharge_order_status"),

    # Stripe webhook (payment confirmation → reactivation)
    path("webhook/", stripe_webhook, name="recharge_webhook"),

    # Admin / dashboard
    path("modules/", RechargeModulesView.as_view(), name="recharge_modules"),
    path("stats/", RechargeStatsView.as_view(), name="recharge_stats"),
    path("transatel-logs/", TransatelLogsView.as_view(), name="recharge_transatel_logs"),
]
