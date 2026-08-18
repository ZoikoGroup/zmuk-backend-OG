"""URL routes for the SIM ordering API.

Mounted in the PROJECT urls (core/urls.py) as:

    # core/urls.py
    path("api/v1/sims/", include("apps.sims.urls")),

With flat app-level paths (no inner "sims/"), that resolves to:

    GET  /api/v1/sims/availability/
    POST /api/v1/sims/availability/latest/   <- ProcessOrder step 1
    POST /api/v1/sims/reserve/
    POST /api/v1/sims/release/
    POST /api/v1/sims/sim-orders/            <- ProcessOrder step 3 (activate)
    GET  /api/v1/sims/orders/<order_reference>/   <- order section: one order + SIMs/QR
    POST /api/v1/sims/orders/by-user/             <- order section: { email } -> orders[]

NOTE: the two Next.js proxy routes must target these exact paths --
  app/api/transatel/sim/availability/route.ts  -> /api/v1/sims/availability/latest/
  app/api/transatel/sim-orders/route.ts        -> /api/v1/sims/sim-orders/

A storefront "order section" page should add matching proxy routes for the
two orders/ endpoints below (same thin pass-through pattern), e.g.:
  app/api/transatel/orders/[orderReference]/route.ts -> /api/v1/sims/orders/<order_reference>/
  app/api/transatel/orders/by-user/route.ts          -> /api/v1/sims/orders/by-user/

IMPORTANT: this file is the ONLY place `path(...)` / `urlpatterns` may live.
Do not paste these lines into api.py -- api.py defines the view classes only.
"""

from django.urls import path

from . import api

app_name = "sims"

urlpatterns = [
    path("availability/",        api.AvailabilityView.as_view(),         name="availability"),
    path("availability/latest/", api.LatestAvailableIccidView.as_view(), name="availability-latest"),
    path("reserve/",             api.ReserveView.as_view(),              name="reserve"),
    path("release/",             api.ReleaseView.as_view(),              name="release"),
    path("sim-orders/",          api.SimOrderView.as_view(),             name="sim-orders"),
    path("orders/by-user/",              api.OrdersByUserView.as_view(), name="orders-by-user"),
    path("orders/<str:order_reference>/", api.OrderDetailView.as_view(), name="order-detail"),
]