import logging
import threading

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PublicSectorRegistration
from .serializers import PublicSectorRegistrationSerializer


logger = logging.getLogger(__name__)


# EMAIL FUNCTION
def send_registration_email(registration_id):

    try:

        registration = (
            PublicSectorRegistration.objects
            .prefetch_related("nominees")
            .get(pk=registration_id)
        )

        nominees = list(registration.nominees.all())

        if nominees:
            nominee_text = "\n\n".join(
                f"{i}. {n.full_name} ({n.relationship})\n"
                f"   Email: {n.email}\n"
                f"   Phone: {n.phone}"
                for i, n in enumerate(nominees, start=1)
            )
        else:
            nominee_text = "No nominees added."

        subject = f"New Public Sector Registration: {registration.full_name}"

        message = f"""
A new public sector worker registration has been submitted.

Registration ID: {registration.id}
Submitted: {registration.created_at:%d %b %Y, %H:%M}

PERSONAL DETAILS
Full Name: {registration.full_name}
Email: {registration.email}
Phone: {registration.phone}
Date of Birth: {registration.date_of_birth:%d %b %Y}

EMPLOYMENT DETAILS
Employer: {registration.employer_name}
Job Title: {registration.job_title}
Work Email: {registration.work_email}
Department: {registration.department}

NOMINATED FAMILY AND FRIENDS ({len(nominees)})
{nominee_text}

Please review this registration in the admin panel.
"""

        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
            [settings.TEST_RECEIVER_EMAIL],
            fail_silently=False,
        )

        # Update only this column so concurrent admin edits aren't overwritten
        PublicSectorRegistration.objects.filter(pk=registration_id).update(
            email_sent=True
        )

        logger.info("Public sector registration email sent (id=%s)", registration_id)

    except Exception:

        logger.exception(
            "Failed to send public sector registration email (id=%s)",
            registration_id,
        )


class PublicSectorRegistrationCreateView(APIView):

    # Public form: no login needed, and no session auth means no CSRF check
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):

        serializer = PublicSectorRegistrationSerializer(data=request.data)

        if not serializer.is_valid():

            return Response(
                {
                    "success": False,
                    "message": "Please fix the highlighted fields and try again.",
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        registration = serializer.save()

        # BACKGROUND EMAIL THREAD (starts only after the data is committed)
        transaction.on_commit(
            lambda: threading.Thread(
                target=send_registration_email,
                args=[registration.pk],
                daemon=True,
            ).start()
        )

        return Response(
            {
                "success": True,
                "message": (
                    "Registration submitted. "
                    "We'll email you once it's been reviewed."
                ),
                "data": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )