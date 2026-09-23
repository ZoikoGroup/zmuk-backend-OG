import threading

from django.conf import settings
from django.core.mail import send_mail

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .serializers import TravelPartnerSerializer


# EMAIL FUNCTION
def send_travel_partner_email(partner):

    try:

        subject = (
            "New Travel Partner Inquiry"
        )

        message = f"""
A new travel partner inquiry has been submitted.

Full Name: {partner.full_name}

Company Name: {partner.company_name}

Business Email: {partner.business_email}

Role: {partner.role}

Website: {partner.website}

Message:
{partner.message}
"""

        send_mail(
            subject,
            message,
            settings.EMAIL_HOST_USER,
            [settings.TEST_RECEIVER_EMAIL],
            fail_silently=False,
        )

        # UPDATE STATUS
        partner.email_sent = True

        partner.save()

        print(
            "Travel partner email sent successfully"
        )

    except Exception as e:

        print("Email Error:", e)


class TravelPartnerCreateView(APIView):

    def post(self, request):

        serializer = (
            TravelPartnerSerializer(
                data=request.data
            )
        )

        if serializer.is_valid():

            serializer.save()

            partner = serializer.instance

            # BACKGROUND EMAIL
            threading.Thread(
                target=send_travel_partner_email,
                args=[partner],
                daemon=True
            ).start()

            return Response(
                {
                    "success": True,
                    "message": (
                        "Travel partner form "
                        "submitted successfully."
                    ),
                    "data": serializer.data,
                },
                status=status.HTTP_201_CREATED
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )