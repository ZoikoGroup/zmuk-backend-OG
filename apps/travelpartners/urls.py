from django.urls import path
from .views import TravelPartnerCreateView


urlpatterns = [
    path(
        "travel-partners/",
        TravelPartnerCreateView.as_view(),
        name="travel-partners"
    ),
]