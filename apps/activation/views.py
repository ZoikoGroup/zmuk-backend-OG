from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework import status

from .serializers import (
    SimActivationSerializer,
    OtpRequestSerializer,
    OtpVerifySerializer,
)
from .utils import (
    create_and_send_otp,
    verify_otp,
    consume_verified_otp,
    can_resend,
    OTP_TTL_MINUTES,
)


class OtpRequestView(APIView):
    """POST /api/otp/request/  { email }  -> emails a 6-digit code."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = OtpRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        if not can_resend(email):
            return Response(
                {"detail": "Please wait a minute before requesting another code."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        try:
            create_and_send_otp(email)
        except Exception as exc:
            # TEMP DEBUG: print the full traceback to the runserver terminal and
            # surface the real error to the browser. Revert the `detail` line to a
            # generic message before production (raw SMTP errors shouldn't leak).
            import traceback
            traceback.print_exc()
            return Response(
                {"detail": f"{type(exc).__name__}: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "message": "A 6-digit code has been sent to your email.",
                "expires_in": OTP_TTL_MINUTES * 60,
            },
            status=status.HTTP_200_OK,
        )


class OtpVerifyView(APIView):
    """POST /api/otp/verify/  { email, otp }  -> { verified: true } on success."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = OtpVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"]
        code = serializer.validated_data["otp"]

        ok, message = verify_otp(email, code)
        if not ok:
            return Response({"detail": message}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {"verified": True, "message": message},
            status=status.HTTP_200_OK,
        )


class SimActivationView(APIView):
    """POST /api/activate/  -> saves only if a verified OTP exists for the email."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SimActivationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]

        # ENFORCED server-side: a verified, unused, unexpired OTP must exist.
        if not consume_verified_otp(email):
            return Response(
                {"detail": "Please verify your email with the OTP before submitting."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer.save()

        return Response(
            {"message": "SIM activation request submitted successfully."},
            status=status.HTTP_201_CREATED,
        )