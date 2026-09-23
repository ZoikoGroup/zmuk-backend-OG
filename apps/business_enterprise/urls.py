from django.urls import path

from .views import EnterpriseInquiryCreateAPIView


urlpatterns = [
    path(
        "enterprise-inquiry/",
        EnterpriseInquiryCreateAPIView.as_view(),
        name="enterprise-inquiry",
    ),
]