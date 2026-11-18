import threading
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.plans.eligibility import discounted_prices
from apps.plans.serializers import PlanSerializer

from .models import StudentDiscountApplication
from .serializers import StudentDiscountApplicationSerializer
from .services import active_offer, expire_lapsed, get_status, send_received_emails, student_plans


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

    The student plans (Audience = Students) with their normal prices; anyone
    can buy them. Once the logged-in student is approved (and not expired),
    each plan also has `discounted_prices` (the offer % off every term
    price), which is what checkout will charge them.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        verification = get_status(request.user)
        offer = active_offer()
        discount_applies = verification["status"] == "approved" and offer is not None
        percent = offer.discount_percent if offer else Decimal("0")

        plans = student_plans().select_related("category").prefetch_related("features")
        data = PlanSerializer(plans, many=True).data
        for plan_data in data:
            plan_data["discounted_prices"] = (
                discounted_prices(plan_data, percent) if discount_applies else None
            )

        return Response({
            "success": True,
            "data": {
                "status": verification["status"],
                "discount_percent": f"{percent:.2f}",
                # True when this student gets the discount at checkout.
                "discount_applies": discount_applies,
                "can_purchase": discount_applies,  # kept for older builds; same as discount_applies
                "plans": data,
            },
        })
