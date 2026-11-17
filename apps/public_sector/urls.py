from django.urls import path

from .views import (
    MyNomineesView,
    MyVerificationStatusView,
    PublicSectorPlansView,
    PublicSectorRegistrationCreateView,
)

urlpatterns = [
    path(
        "public-sector-workers/",
        PublicSectorRegistrationCreateView.as_view(),
        name="public-sector-workers",
    ),
    path(
        "public-sector-workers/me/",
        MyVerificationStatusView.as_view(),
        name="public-sector-my-status",
    ),
    path(
        "public-sector-workers/me/nominees/",
        MyNomineesView.as_view(),
        name="public-sector-my-nominees",
    ),
    path(
        "public-sector-workers/plans/",
        PublicSectorPlansView.as_view(),
        name="public-sector-plans",
    ),
]
