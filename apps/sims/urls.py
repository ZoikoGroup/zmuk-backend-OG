"""URL routes for the SIM ordering API.

Wire into the project urls, e.g.:

    # project/urls.py
    urlpatterns = [
        ...
        path("api/v1/", include("apps.sims.urls")),
    ]

which exposes:

    GET  /api/v1/sims/availability/
    POST /api/v1/sims/reserve/
    POST /api/v1/sims/release/
    POST /api/v1/sim-orders/
"""

from django.urls import path

from . import api

app_name = "sims"

urlpatterns = [
    path("sims/availability/", api.AvailabilityView.as_view(), name="availability"),
    path("sims/reserve/", api.ReserveView.as_view(), name="reserve"),
    path("sims/release/", api.ReleaseView.as_view(), name="release"),
    path("sim-orders/", api.SimOrderView.as_view(), name="sim-orders"),
]
