"""Routes mounted under /api/v1/ in core/urls.py."""

from django.urls import path

from . import views

app_name = "roaming"

urlpatterns = [
    path("roaming/destinations/", views.DestinationListView.as_view(), name="destinations"),
    path("roaming/destinations/<str:code>/", views.DestinationRatesView.as_view(), name="destination-rates"),
    path("lines/<uuid:line_id>/roaming-quote/", views.LineRoamingQuoteView.as_view(), name="line-quote"),
]
