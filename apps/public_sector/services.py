"""Public sector verification: status, the % discount offer and decision emails."""

import logging

from django.conf import settings
from django.core.mail import send_mail

from apps.plans.eligibility import apply_discount  # noqa: F401  (shared rounding, re-exported)

from .models import PublicSectorOffer, PublicSectorRegistration

logger = logging.getLogger(__name__)

Status = PublicSectorRegistration.Status


# ─── OFFER (30% off selected normal plans for verified workers) ─────────────

def active_offer():
    """The live offer, or None when there's no offer or it's paused."""
    return PublicSectorOffer.objects.filter(is_active=True).order_by("pk").first()


def offer_plans(offer):
    """Active plans covered by the offer (none when there's no offer)."""
    from apps.plans.models import Plan

    if offer is None:
        return Plan.objects.none()
    return offer.plans.filter(is_active=True, audience=Plan.Audience.GENERAL)


def is_approved(user) -> bool:
    return get_status(user)["status"] == Status.APPROVED


def current_registration(user):
    """The user's open registration if they have one, otherwise their latest."""
    if not user or not user.is_authenticated:
        return None
    registrations = PublicSectorRegistration.objects.filter(user=user).order_by(
        "-created_at", "-pk"
    )
    return (
        registrations.filter(status__in=PublicSectorRegistration.OPEN_STATUSES).first()
        or registrations.first()
    )


def get_status(user, registration=None) -> dict:
    """{status: none|pending|approved|rejected, application: {...} | None}."""
    registration = registration or current_registration(user)
    if registration is None:
        return {"status": "none", "application": None}
    return {
        "status": registration.status,
        "application": {
            "id": registration.pk,
            "submitted_at": registration.created_at,
            "reviewed_at": registration.reviewed_at,
            "rejection_reason": (
                registration.rejection_reason
                if registration.status == Status.REJECTED
                else ""
            ),
        },
    }


def send_decision_email(registration_id):
    """Tell the customer their registration was approved or rejected (runs in a thread)."""
    try:
        registration = PublicSectorRegistration.objects.get(pk=registration_id)
        if registration.status == Status.APPROVED:
            subject = "Your public sector registration is approved"
            body = (
                "Good news: your public sector worker registration has been approved.\n\n"
                "You can now choose a public sector plan from your Zoiko Mobile account."
            )
        elif registration.status == Status.REJECTED:
            subject = "Your public sector registration"
            reason = registration.rejection_reason or "No reason was given."
            body = (
                "We couldn't approve your public sector worker registration.\n\n"
                f"Reason: {reason}\n\n"
                "You can send a new registration from your Zoiko Mobile account."
            )
        else:
            return

        send_mail(
            subject,
            f"Hi {registration.full_name},\n\n{body}\n\nRegards,\nZoiko Mobile\n",
            settings.DEFAULT_FROM_EMAIL or settings.EMAIL_HOST_USER,
            [registration.email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Failed to send public sector decision email (id=%s)", registration_id)
