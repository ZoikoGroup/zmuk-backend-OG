from django.urls import path
from .views import EnterpriseInquiryCreateView

urlpatterns = [
    path(
        "enterprise-inquiry/",
        EnterpriseInquiryCreateView.as_view(),
        name="enterprise-inquiry"
    ),
]