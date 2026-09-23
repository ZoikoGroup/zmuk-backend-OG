from django.urls import path
from .views import RequestDemoView

urlpatterns = [
    path('request-a-demo/', RequestDemoView.as_view()),
]