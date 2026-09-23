from django.urls import path

from .views import (
    TravelEcosystemPartnerInquiryCreateAPIView
)


urlpatterns = [
    path(
        "travel-ecosystem-inquiry/",
        TravelEcosystemPartnerInquiryCreateAPIView.as_view(),
        name="travel-ecosystem-inquiry",
    ),
]