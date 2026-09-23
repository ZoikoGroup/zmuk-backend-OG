from django.urls import path
from .views import IntegrationInterestView

urlpatterns = [
    path('integration-interest/', IntegrationInterestView.as_view(), name='integration-interest'),
]