from django.urls import path
from .views import RequestFormAPIView

urlpatterns = [
    path('request-form', RequestFormAPIView.as_view(), name='request-form'),
]