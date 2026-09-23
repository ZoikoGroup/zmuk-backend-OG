from django.urls import path
from .views import MilitaryDiscountEnrollmentAPIView

urlpatterns = [
    path(
        "enrollment",
        MilitaryDiscountEnrollmentAPIView.as_view(),
        name="military-enrollment"
    ),
]
