from django.urls import path
from .views import FirstResponderCreateAPIView

urlpatterns = [
    path(
        "first-responder-discount-form",
        FirstResponderCreateAPIView.as_view(),
        name="first_responder_form"
    ),
]
