from django.urls import path
from .views import JobApplicationCreateView

urlpatterns = [
    path("apply_job/", JobApplicationCreateView.as_view(), name="apply_job"),
]