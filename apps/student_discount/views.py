import threading

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.plans.models import Plan
from apps.plans.serializers import PlanSerializer

from .models import StudentDiscountApplication
from .serializers import StudentDiscountApplicationSerializer
from .services import expire_lapsed, get_status, send_received_emails


def _already_applied():
    return Response(
        {
            "code": "ALREADY_APPLIED",
            "message": "You already have a student application that is pending or approved.",
        },
        status=status.HTTP_409_CONFLICT,
    )


class StudentDiscountApplicationView(APIView):
    """POST /api/student-discount/  (login required, multipart)

    Saves the application against the logged-in user.
    409 ALREADY_APPLIED if the user already has an open application.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):

        serializer = StudentDiscountApplicationSerializer(
            data=request.data
        )

        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                # Lock the user so two submissions at once can't both pass the check.
                get_user_model().objects.select_for_update().filter(pk=request.user.pk).first()
                # An approval past its graduation date no longer blocks a new application.
                expire_lapsed(request.user)

                if request.user.student_discount_applications.filter(
                    status__in=StudentDiscountApplication.OPEN_STATUSES
                ).exists():
                    return _already_applied()

                application = serializer.save(user=request.user)

                # Emails go out after the commit, without holding up the response.
                transaction.on_commit(
                    lambda: threading.Thread(
                        target=send_received_emails, args=[application.pk], daemon=True
                    ).start()
                )
        except IntegrityError:
            return _already_applied()

        return Response(
            {
                "message": "Application submitted successfully.",
                "data": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )


class MyStudentStatusView(APIView):
    """GET /api/student-discount/me/

    {status: none|pending|approved|rejected|expired, application: {...} | null}
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"success": True, "data": get_status(request.user)})


class StudentPlansView(APIView):
    """GET /api/student-discount/plans/

    Student plans for everyone; can_purchase is true only while the
    logged-in user's application is approved (and not expired).
    """

    permission_classes = [AllowAny]

    def get(self, request):
        verification = get_status(request.user)
        plans = Plan.objects.filter(
            is_active=True, audience=Plan.Audience.STUDENT
        ).select_related("category").prefetch_related("features")

        return Response({
            "success": True,
            "data": {
                "status": verification["status"],
                "can_purchase": verification["status"] == "approved",
                "plans": PlanSerializer(plans, many=True).data,
            },
        })
