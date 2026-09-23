"""
URL routing for the Malima app.

Mount in your project's root urls.py:

    from django.urls import path, include

    urlpatterns = [
        ...
        path("api/v1/", include("malima.urls")),
    ]

Routes (matching the env-var defaults in the Next.js patch):

    POST /api/v1/malima/allocate-sims/   → AllocateSimsView
    POST /api/v1/malima-orders/          → MalimaOrderView
    POST /api/v1/malima/release/         → ReleaseReservationsView   (ops)
"""

from django.urls import path

from .views import AllocateSimsView, MalimaOrderView, ReleaseReservationsView

app_name = "malima"

urlpatterns = [
    path("malima/allocate-sims/", AllocateSimsView.as_view(), name="allocate-sims"),
    path("malima-orders/", MalimaOrderView.as_view(), name="orders"),
    path("malima/release/", ReleaseReservationsView.as_view(), name="release"),
]
