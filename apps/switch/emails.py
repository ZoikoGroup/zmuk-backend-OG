"""Branded HTML email for Switch & Save (same pattern as the accounts app)."""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger("apps.switch")


def ops_recipient() -> str:
    return (
        getattr(settings, "SWITCH_OPS_EMAIL", None)
        or getattr(settings, "BLOG_NOTIFICATION_ADMIN_EMAIL", None)
        or getattr(settings, "DEFAULT_FROM_EMAIL", None)
        or "info@zoikomobile.co.uk"
    )


def send_html_email(subject, template_name, context, recipient, attachments=()) -> bool:
    """Send one email. Returns True once the mail backend accepted it, False
    on failure (logged without the recipient). Callers record the outcome;
    a False must never be shown to the customer as "sent"."""
    try:
        html_body = render_to_string(template_name, context)
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@zoikomobile.co.uk"
        msg = EmailMultiAlternatives(
            subject=subject, body=strip_tags(html_body), from_email=from_email, to=[recipient],
        )
        msg.attach_alternative(html_body, "text/html")
        for filename, content, mimetype in attachments:
            msg.attach(filename, content, mimetype)
        msg.send()
        return True
    except Exception:
        logger.exception("Failed to send switch email '%s' (recipient not logged)", subject)
        return False
