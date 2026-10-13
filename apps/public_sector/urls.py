from django.urls import path

from .views import PublicSectorRegistrationCreateView

urlpatterns = [
    path(
        "public-sector-workers/",
        PublicSectorRegistrationCreateView.as_view(),
        name="public-sector-workers",
    ),
]