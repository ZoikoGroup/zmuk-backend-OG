import logging
import threading
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import IntegrityError, transaction

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.plans.serializers import PlanSerializer

from .models import MAX_NOMINEES, PublicSectorRegistration
from .serializers import (
    NomineeSerializer,
    PublicSectorRegistrationSerializer,
    digits_only,
    nominee_conflicts,
)
from apps.plans.eligibility import discounted_prices

from .services import active_offer, current_registration, get_status, offer_plans


logger = logging.getLogger(__name__)


def _error(code, message, http_status, errors=None):
    body = {"success": False, "code": code, "message": message}
    if errors is not None:
        body["errors"] = errors
    return Response(body, status=http_status)


def _in_background(func, *args):
    # Starts only after the data is committed.
    transaction.on_commit(
        lambda: threading.Thread(target=func, args=args, daemon=True).start()
    )


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


# ─── REGISTRATION ────────────────────────────────────────────────────────────

class PublicSectorRegistrationCreateView(APIView):
    """POST /api/public-sector-workers/  (login required)

    Saves the registration (with up to 5 nominees) against the logged-in user.
    409 ALREADY_APPLIED if the user already has a pending or approved one.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):

        serializer = PublicSectorRegistrationSerializer(data=request.data)

        if not serializer.is_valid():

            return _error(
                "VALIDATION_ERROR",
                "Please fix the highlighted fields and try again.",
                status.HTTP_400_BAD_REQUEST,
                serializer.errors,
            )

        try:
            with transaction.atomic():
                # Lock the user so two submissions at once can't both pass the check.
                get_user_model().objects.select_for_update().filter(pk=request.user.pk).first()

                if request.user.public_sector_registrations.filter(
                    status__in=PublicSectorRegistration.OPEN_STATUSES
                ).exists():
                    return _already_applied()

                registration = serializer.save(user=request.user)
                _in_background(send_registration_email, registration.pk)
        except IntegrityError:
            return _already_applied()

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


def _already_applied():
    return _error(
        "ALREADY_APPLIED",
        "You already have a registration that is pending or approved.",
        status.HTTP_409_CONFLICT,
    )


class MyVerificationStatusView(APIView):
    """GET /api/public-sector-workers/me/

    {status: none|pending|approved|rejected, application: {...} | null}
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"success": True, "data": get_status(request.user)})


# ─── PLANS ───────────────────────────────────────────────────────────────────

class PublicSectorPlansView(APIView):
    """GET /api/public-sector-workers/plans/

    The normal plans covered by the public sector offer, with their normal
    prices. Once the logged-in user's registration is approved, each plan
    also has `discounted_prices` (the offer % off every term price), which
    is what checkout will charge them.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        verification = get_status(request.user)
        offer = active_offer()
        approved = verification["status"] == PublicSectorRegistration.Status.APPROVED
        discount_applies = approved and offer is not None
        percent = offer.discount_percent if offer else Decimal("0")

        plans = (
            offer_plans(offer).select_related("category").prefetch_related("features")
        )
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
                # True when this user gets the discount at checkout.
                "discount_applies": discount_applies,
                "can_purchase": discount_applies,  # kept for older builds; same as discount_applies
                "plans": data,
            },
        })


# ─── NOMINEES ────────────────────────────────────────────────────────────────

def _nominee_summary(registration, nominees):
    used = len(nominees)
    return {
        "used": used,
        "limit": MAX_NOMINEES,
        "remaining": max(0, MAX_NOMINEES - used),
        "can_add": (
            registration.status in PublicSectorRegistration.OPEN_STATUSES
            and used < MAX_NOMINEES
        ),
        "nominees": NomineeSerializer(nominees, many=True).data,
    }


class MyNomineesView(APIView):
    """GET / POST /api/public-sector-workers/me/nominees/

    GET:  {used, limit, remaining, can_add, nominees: [...]}
    POST: {full_name, email, phone, relationship} adds one nominee while the
          registration is pending or approved, up to 5 in total.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        registration = current_registration(request.user)
        if registration is None:
            return _no_registration()
        return Response({
            "success": True,
            "data": _nominee_summary(registration, list(registration.nominees.all())),
        })

    def post(self, request):
        serializer = NomineeSerializer(data=request.data)
        if not serializer.is_valid():
            return _error(
                "VALIDATION_ERROR",
                "Please fix the highlighted fields and try again.",
                status.HTTP_400_BAD_REQUEST,
                serializer.errors,
            )

        with transaction.atomic():
            registration = current_registration(request.user)
            if registration is None:
                return _no_registration()
            # Lock the registration so two requests at once can't pass the limit.
            registration = PublicSectorRegistration.objects.select_for_update().get(
                pk=registration.pk
            )

            if registration.status not in PublicSectorRegistration.OPEN_STATUSES:
                return _error(
                    "REGISTRATION_CLOSED",
                    "Nominees can't be added to a rejected registration.",
                    status.HTTP_409_CONFLICT,
                )

            existing = list(registration.nominees.all())
            if len(existing) >= MAX_NOMINEES:
                return _error(
                    "NOMINEE_LIMIT_REACHED",
                    f"You can nominate up to {MAX_NOMINEES} people.",
                    status.HTTP_409_CONFLICT,
                )

            conflicts = nominee_conflicts(
                serializer.validated_data,
                own_emails={registration.email, registration.work_email},
                own_phone=digits_only(registration.phone),
                seen_emails={n.email for n in existing},
                seen_phones={digits_only(n.phone) for n in existing},
            )
            if conflicts:
                return _error(
                    "VALIDATION_ERROR",
                    "Please fix the highlighted fields and try again.",
                    status.HTTP_400_BAD_REQUEST,
                    conflicts,
                )

            nominee = serializer.save(registration=registration)
            existing.append(nominee)

        return Response(
            {
                "success": True,
                "message": "Nominee added.",
                "data": _nominee_summary(registration, existing),
            },
            status=status.HTTP_201_CREATED,
        )


def _no_registration():
    return _error(
        "NO_REGISTRATION",
        "You haven't sent a public sector registration yet.",
        status.HTTP_404_NOT_FOUND,
    )
