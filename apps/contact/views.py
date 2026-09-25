import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings

from .serializers import ContactMessageSerializer

logger = logging.getLogger("apps.contact")


def _send_contact_emails(contact):
    """Send two emails:
    1. Notification to the ops team about the new contact message.
    2. Auto-reply to the customer confirming receipt.
    Falls back silently — never raises.
    """
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@zoikomobile.co.uk"
    ops_email = getattr(settings, "SWITCH_OPS_EMAIL", None) or "info@zoikomobile.co.uk"

    # ── 1. Internal notification to ops ────────────────────────────────────
    try:
        ctx = {
            "name": contact.name,
            "email": contact.email,
            "phone": contact.phone,
            "subject": contact.subject,
            "message": contact.message,
            "created_at": contact.created_at,
        }
        html = render_to_string("emails/contact_notification.html", ctx)
        text = strip_tags(html)

        msg = EmailMultiAlternatives(
            subject=f"New Contact: {contact.subject} — {contact.name}",
            body=text,
            from_email=from_email,
            to=[ops_email],
        )
        msg.attach_alternative(html, "text/html")
        msg.send()
    except Exception:
        logger.exception("Failed to send contact notification for %s", contact.email)

    # ── 2. Auto-reply to the customer ──────────────────────────────────────
    try:
        ctx = {
            "name": contact.name,
            "subject": contact.subject,
        }
        html = render_to_string("emails/contact_autoreply.html", ctx)
        text = strip_tags(html)

        msg = EmailMultiAlternatives(
            subject="We received your message — Zoiko Mobile",
            body=text,
            from_email=from_email,
            to=[contact.email],
        )
        msg.attach_alternative(html, "text/html")
        msg.send()
    except Exception:
        logger.exception("Failed to send contact auto-reply to %s", contact.email)


class ContactUsAPIView(APIView):

    def post(self, request):
        serializer = ContactMessageSerializer(data=request.data)

        if serializer.is_valid():
            contact = serializer.save()

            # Send notification + auto-reply emails
            _send_contact_emails(contact)

            return Response(
                {"message": "Message sent successfully"},
                status=status.HTTP_201_CREATED,
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST,
        )
