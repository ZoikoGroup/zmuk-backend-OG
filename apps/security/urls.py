from django.urls import path
from .views import TwoFAView, ReportView

app_name = "security"

urlpatterns = [
    path("two-fa/", TwoFAView.as_view(), name="two-fa"),
    path("report/", ReportView.as_view(), name="report"),
]
