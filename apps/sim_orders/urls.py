from django.urls import path

from .views import (
    SimProductsView, BuySimView, SimOrdersView, stripe_webhook,
    SimCheckoutOrderView, SimCheckoutOrdersByUserView,
)

urlpatterns = [
    path("products/", SimProductsView.as_view(), name="sim_products"),
    path("buy/", BuySimView.as_view(), name="sim_buy"),
    path("orders/", SimOrdersView.as_view(), name="sim_orders"),
    path("webhook/", stripe_webhook, name="sim_webhook"),
    path("checkout-order/", SimCheckoutOrderView.as_view(), name="sim_checkout_order"),
    path("checkout-orders/by-user/", SimCheckoutOrdersByUserView.as_view(), name="sim_checkout_orders_by_user"),
]