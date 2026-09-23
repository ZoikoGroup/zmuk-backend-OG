import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from rest_framework import generics, permissions, status
from rest_framework.response import Response

from .models import SwitchRequest
from .serializers import SwitchRequestSerializer

logger = logging.getLogger("apps.switch")


# ── Email helper (same pattern as accounts app) ───────────────────────────────

def _send_html_email(subject, template_name, context, recipient):
    """Send branded HTML email. Returns True on success, False on failure (logged)."""
    try:
        html_body = render_to_string(template_name, context)
        text_body = strip_tags(html_body)
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@zoikomobile.co.uk"

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=from_email,
            to=[recipient],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send()
        return True
    except Exception:
        logger.exception("Failed to send switch email '%s' to %s", subject, recipient)
        return False


# ── Views ─────────────────────────────────────────────────────────────────────

class SwitchRequestCreateView(generics.CreateAPIView):
    """Public endpoint that the 'Switch to Zoiko Mobile' form POSTs to."""

    queryset = SwitchRequest.objects.all()
    serializer_class = SwitchRequestSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()

        ctx = {
            "first_name":             instance.first_name,
            "last_name":              instance.last_name,
            "email":                  instance.email,
            "mobile":                 instance.mobile,
            "postcode":               instance.postcode,
            "current_provider":       instance.current_provider or "",
            "current_plan_cost":      instance.current_plan_cost,
            "current_data_allowance": instance.current_data_allowance,
            "selected_plan":          instance.selected_plan,
            "created_at":             instance.created_at.strftime("%d %b %Y, %H:%M UTC"),
        }

        # 1) Confirmation email → customer
        _send_html_email(
            subject="Your Zoiko Mobile switch request is confirmed",
            template_name="emails/switch_request_customer.html",
            context=ctx,
            recipient=instance.email,
        )

        # 2) Ops notification → internal team
        ops_email = (
            getattr(settings, "SWITCH_OPS_EMAIL", None)
            or getattr(settings, "BLOG_NOTIFICATION_ADMIN_EMAIL", None)
            or getattr(settings, "DEFAULT_FROM_EMAIL", None)
            or "info@zoikomobile.co.uk"
        )
        _send_html_email(
            subject=f"New Switch Request — {instance.first_name} {instance.last_name} → {instance.selected_plan}",
            template_name="emails/switch_request_ops.html",
            context=ctx,
            recipient=ops_email,
        )

        return Response(
            {
                "success": True,
                "message": (
                    "Your switch request has been received. "
                    "We'll be in touch within 1 working day."
                ),
                "data": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )


class SwitchRequestListView(generics.ListAPIView):
    """Staff-only list of submitted switch requests."""

    queryset = SwitchRequest.objects.all()
    serializer_class = SwitchRequestSerializer
    permission_classes = [permissions.IsAdminUser]


class SwitchRequestDetailView(generics.RetrieveAPIView):
    """Staff-only detail view of a single switch request."""

    queryset = SwitchRequest.objects.all()
    serializer_class = SwitchRequestSerializer
    permission_classes = [permissions.IsAdminUser]
