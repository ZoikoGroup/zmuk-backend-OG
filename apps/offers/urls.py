"""Routes mounted under /api/v1/ in core/urls.py."""
from django.urls import path

from .views import OfferListView

app_name = "offers"

urlpatterns = [
    path("offers/", OfferListView.as_view(), name="list"),
]
