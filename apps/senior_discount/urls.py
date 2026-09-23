from django.urls import path
from .views import SeniorCitizenDiscountCreateAPI

urlpatterns = [
    path(
        "senior-citizen-discount",
        SeniorCitizenDiscountCreateAPI.as_view(),
    ),
]
