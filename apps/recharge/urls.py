from django.urls import path

from .views import (
    RechargeModulesView,
    RechargeStatsView,
    RechargeOrdersView,
    CreateRechargeView,
    stripe_webhook,
)

urlpatterns = [
    path("modules/", RechargeModulesView.as_view(), name="recharge_modules"),
    path("stats/", RechargeStatsView.as_view(), name="recharge_stats"),
    path("orders/", RechargeOrdersView.as_view(), name="recharge_orders"),
    path("create/", CreateRechargeView.as_view(), name="recharge_create"),
    path("webhook/", stripe_webhook, name="recharge_webhook"),
]
