"""Routes for /api/v1/lines/ (mounted in core/urls.py)."""

from django.urls import path

from . import views, webhooks

app_name = "add_line"

urlpatterns = [
    path("", views.LinesView.as_view(), name="lines"),
    path("claims/", views.ClaimsView.as_view(), name="claims"),
    path("pac/check/", views.PacCheckView.as_view(), name="pac-check"),
    path("<uuid:line_id>/port/", views.LinePortView.as_view(), name="line-port"),
    path("webhooks/transatel/", webhooks.transatel_webhook, name="transatel-webhook"),
    path("eligibility/", views.EligibilityView.as_view(), name="eligibility"),
    path("quote/", views.QuoteView.as_view(), name="quote"),
    path("checkout/", views.CheckoutView.as_view(), name="checkout"),
    path("orders/<str:order_reference>/", views.OrderDetailView.as_view(), name="order-detail"),
    path("orders/<str:order_reference>/confirm/", views.ConfirmPaymentView.as_view(), name="order-confirm"),
    path("webhooks/stripe/", views.stripe_webhook, name="stripe-webhook"),
]
