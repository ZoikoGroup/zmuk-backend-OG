from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from .models import UserSecurity
from .serializers import TwoFASerializer, SuspiciousReportSerializer


class TwoFAView(APIView):
    """PATCH /api/security/two-fa/  body: {"enabled": bool}"""
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        serializer = TwoFASerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        sec, _ = UserSecurity.objects.get_or_create(user=request.user)
        sec.two_fa_enabled = serializer.validated_data["enabled"]
        sec.save()

        return Response({"enabled": sec.two_fa_enabled})

    def get(self, request):
        sec, _ = UserSecurity.objects.get_or_create(user=request.user)
        return Response({"enabled": sec.two_fa_enabled})


class ReportView(APIView):
    """POST /api/security/report/  body: {"note": str}"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = SuspiciousReportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(user=request.user)
        return Response(
            {"message": "Report submitted", "report": serializer.data},
            status=status.HTTP_201_CREATED,
        )
