from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from .models import SupportTicket
from .serializers import SupportTicketSerializer, CallbackRequestSerializer


class TicketView(APIView):
    """
    POST /api/support/tickets/  body: {subject, category, message}
    GET  /api/support/tickets/  -> this user's tickets
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tickets = SupportTicket.objects.filter(user=request.user)
        return Response(SupportTicketSerializer(tickets, many=True).data)

    def post(self, request):
        serializer = SupportTicketSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(user=request.user)
        return Response(
            {"message": "Ticket submitted", "ticket": serializer.data},
            status=status.HTTP_201_CREATED,
        )


class CallbackView(APIView):
    """POST /api/support/callback/  body: {phone}"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CallbackRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(user=request.user)
        return Response(
            {"message": "Call-back requested", "request": serializer.data},
            status=status.HTTP_201_CREATED,
        )
