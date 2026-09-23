import threading

from django.conf import settings
from django.core.mail import send_mail

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .serializers import EnterpriseInquirySerializer


# EMAIL FUNCTION
def send_inquiry_email(inquiry):

    try:

        subject = "New Enterprise Inquiry"

        message = f"""
A new enterprise inquiry has been submitted.

First Name: {inquiry.first_name}

Last Name: {inquiry.last_name}

Business Email: {inquiry.business_email}

Company Name: {inquiry.company_name}

Phone: {inquiry.phone}

Country: {inquiry.country}

Inquiry Type: {inquiry.inquiry_type}

Message:
{inquiry.message}
"""

        send_mail(
            subject,
            message,
            settings.EMAIL_HOST_USER,
            [settings.TEST_RECEIVER_EMAIL],
            fail_silently=False,
        )

        # UPDATE STATUS
        inquiry.email_sent = True

        inquiry.save()

        print("Email sent successfully")

    except Exception as e:

        print("Email Error:", e)


class EnterpriseInquiryCreateView(APIView):

    def post(self, request):

        serializer = (
            EnterpriseInquirySerializer(
                data=request.data
            )
        )

        if serializer.is_valid():

            serializer.save()

            inquiry = serializer.instance

            # BACKGROUND EMAIL THREAD
            threading.Thread(
                target=send_inquiry_email,
                args=[inquiry],
                daemon=True
            ).start()

            return Response(
                {
                    "success": True,
                    "message": (
                        "Inquiry submitted successfully"
                    ),
                    "data": serializer.data,
                },
                status=status.HTTP_201_CREATED
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )