from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import OfferCardSerializer
from .services import live_offers

CACHE_SECONDS = 60


class OfferListView(APIView):
    """GET /api/v1/offers/: the limited-time offers live right now.

    Everyone gets the same list, so no login is needed. Authentication is
    switched off on purpose: the page sends its token when it has one, and an
    expired token must not turn this read into a 401.

    The page uses `server_time` to run the countdown on the server's clock.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        now = timezone.now()
        response = Response({
            "server_time": now,
            "results": OfferCardSerializer(live_offers(now), many=True).data,
        })
        response["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
        return response
