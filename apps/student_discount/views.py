from django.conf import settings
from django.core.mail import send_mail

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import StudentDiscountApplicationSerializer


class StudentDiscountApplicationView(APIView):

    permission_classes = []

    authentication_classes = []

    def post(self, request):

        serializer = StudentDiscountApplicationSerializer(
            data=request.data
        )

        if serializer.is_valid():

            application = serializer.save()

            # -------------------------
            # Email to Admin
            # -------------------------

            send_mail(
                subject="New Student Discount Application",
                message=f"""
A new student discount application has been submitted.

Name: {application.full_name}

Email: {application.email}

Mobile: {application.mobile}

Institution:
{application.institution}

Plan:
{application.selected_plan}

Contract:
{application.contract_duration}

Status:
{application.status}
                """,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[settings.DEFAULT_FROM_EMAIL],
                fail_silently=True,
            )

            # -------------------------
            # Email to Student
            # -------------------------

            send_mail(
                subject="Student Discount Application Received",
                message=f"""
Hi {application.full_name},

Thank you for submitting your Student Discount Application.

Our team has successfully received your application.

We will review it and contact you shortly.

Thank you for choosing Zoiko Mobile.

Regards,

Zoiko Mobile
                """,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[application.email],
                fail_silently=True,
            )

            return Response(
                {
                    "message": "Application submitted successfully."
                },
                status=status.HTTP_201_CREATED,
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST,
        )