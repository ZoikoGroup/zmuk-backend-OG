from django.urls import path

from .views import SimActivationView, OtpRequestView, OtpVerifyView


urlpatterns = [
    path("otp/request/", OtpRequestView.as_view(), name="otp-request"),
    path("otp/verify/", OtpVerifyView.as_view(), name="otp-verify"),
    path("activate/", SimActivationView.as_view(), name="activate-sim"),
]