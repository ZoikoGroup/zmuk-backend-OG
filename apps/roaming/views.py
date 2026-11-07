from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.add_line.models import Line

from .models import Destination
from .serializers import DestinationSerializer, LineQuoteParamsSerializer, TravelDateSerializer
from .services import quote, register

SEARCH_LIMIT = 25


def _error(code, message, http_status):
    return Response({"code": code, "message": message}, status=http_status)


class DestinationListView(APIView):
    """GET /api/v1/roaming/destinations/?q=fra  → destinations for the country picker.

    Matches name, ISO code or alias. `rating_zone` is the published charging
    zone only; it says nothing about whether roaming is included on a plan.
    The UK itself is left out because using a phone there isn't roaming.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        q = request.query_params.get("q", "").strip()
        destinations = Destination.objects.filter(is_active=True).exclude(code=register.DOMESTIC_CODE)
        if q:
            destinations = destinations.filter(
                Q(name__icontains=q) | Q(code__iexact=q) | Q(aliases__icontains=q)
            )[:SEARCH_LIMIT]
        zones = register.published_zones(register.active_version())
        return Response({
            "destinations": DestinationSerializer(destinations, many=True, context={"zones": zones}).data,
        })


class DestinationRatesView(APIView):
    """GET /api/v1/roaming/destinations/<code>/?travel_date=2026-12-01

    Published general rates for customers without an active line. Always
    marked "Not a personalised quote".
    """

    permission_classes = [AllowAny]

    def get(self, request, code):
        params = TravelDateSerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        try:
            destination = quote.get_destination(code)
        except quote.QuoteError as exc:
            return _error(exc.code, exc.message, status.HTTP_404_NOT_FOUND)
        return Response(quote.public_quote(destination, params.validated_travel_date()).data)


class LineRoamingQuoteView(APIView):
    """GET /api/v1/lines/<line_id>/roaming-quote/?country=FR&travel_date=2026-12-01

    The personalised roaming quote for one of the customer's own lines. The
    backend decides the status and outcome; the dashboard only renders them.
    Read-only, and every answer is saved as a `RoamingQuote` for audit.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, line_id):
        line = get_object_or_404(Line.objects.select_related("plan"), uuid=line_id, owner=request.user)
        params = LineQuoteParamsSerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        try:
            destination = quote.get_destination(params.validated_data["country"])
        except quote.QuoteError as exc:
            return _error(exc.code, exc.message, status.HTTP_400_BAD_REQUEST)
        result = quote.line_quote(request.user, line, destination, params.validated_travel_date())
        response = Response(result.data)
        response["Cache-Control"] = "no-store"
        return response
