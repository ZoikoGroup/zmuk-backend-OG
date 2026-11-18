"""Student verification: status, expiry, the % discount offer and decision emails."""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import StudentDiscountApplication, StudentOffer

logger = logging.getLogger(__name__)

APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"

# The frontend sees: none / pending / approved / rejected / expired.
# "Under review" is an admin-only step, so it is shown as pending.
_PUBLIC_STATUS = {"under_review": "pending"}


# ─── OFFER (20% off every student plan for verified students) ───────────────

def active_offer():
    """The live student offer, or None when there's no offer or it's paused."""
    return StudentOffer.objects.filter(is_active=True).order_by("pk").first()


def student_plans():
    from apps.plans.models import Plan

    return Plan.objects.filter(is_active=True, audience=Plan.Audience.STUDENT)


def is_approved(user) -> bool:
    """Approved and not past the graduation date (get_status expires it)."""
    return get_status(user)["status"] == APPROVED


def expire_lapsed(user=None) -> int:
    """Mark approved applications whose graduation date has passed as expired."""
    lapsed = StudentDiscountApplication.objects.filter(
        status=APPROVED, graduation_date__lt=timezone.localdate()
    )
    if user is not None:
        lapsed = lapsed.filter(user=user)
    return lapsed.update(status=EXPIRED, updated_at=timezone.now())


def current_application(user):
    """The user's open application if they have one, otherwise their latest."""
    if not user or not user.is_authenticated:
        return None
    expire_lapsed(user)
    applications = StudentDiscountApplication.objects.filter(user=user).order_by(
        "-created_at", "-pk"
    )
    return (
        applications.filter(status__in=StudentDiscountApplication.OPEN_STATUSES).first()
        or applications.first()
    )


def public_status(status: str) -> str:
    return _PUBLIC_STATUS.get(status, status)


def get_status(user) -> dict:
    """{status: none|pending|approved|rejected|expired, application: {...} | None}."""
    application = current_application(user)
    if application is None:
        return {"status": "none", "application": None}
    return {
        "status": public_status(application.status),
        "application": {
            "id": application.pk,
            "submitted_at": application.created_at,
            "reviewed_at": application.reviewed_at,
            "rejection_reason": (
                application.rejection_reason if application.status == REJECTED else ""
            ),
            # The approval lapses after this date.
            "valid_until": application.graduation_date,
        },
    }


def send_received_emails(application_id):
    """Admin notification + confirmation to the student (runs in a thread)."""
    try:
        application = StudentDiscountApplication.objects.get(pk=application_id)
    except StudentDiscountApplication.DoesNotExist:
        return

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


def send_decision_email(application_id):
    """Tell the student their application was approved or rejected (runs in a thread)."""
    try:
        application = StudentDiscountApplication.objects.get(pk=application_id)
        if application.status == APPROVED:
            subject = "Your student discount application is approved"
            body = (
                "Good news: your student discount application has been approved.\n\n"
                "You can now choose a student plan from your Zoiko Mobile account. "
                f"Your student pricing is available until {application.graduation_date:%d %b %Y}."
            )
        elif application.status == REJECTED:
            subject = "Your student discount application"
            reason = application.rejection_reason or "No reason was given."
            body = (
                "We couldn't approve your student discount application.\n\n"
                f"Reason: {reason}\n\n"
                "You can send a new application from your Zoiko Mobile account."
            )
        else:
            return

        send_mail(
            subject,
            f"Hi {application.full_name},\n\n{body}\n\nRegards,\nZoiko Mobile\n",
            settings.DEFAULT_FROM_EMAIL,
            [application.email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Failed to send student decision email (id=%s)", application_id)
