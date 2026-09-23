from django.urls import path
from .views import MarineDiscountEnrollmentAPIView

urlpatterns = [
    path(
        "enrollment",
        MarineDiscountEnrollmentAPIView.as_view(),
        name="marine-enrollment"
    ),
]
