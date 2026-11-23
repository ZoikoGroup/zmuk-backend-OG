"""Saved payment methods API (/api/v1/payment-methods/).

All endpoints are behind SAVED_PAYMENT_METHODS_ENABLED. While it is off the
list says {"enabled": false} and everything else answers 404.
"""

import logging

import stripe
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.add_line.services.secrets import request_problem, usable_secret
from core.stripe_compat import to_plain

from . import services
from .models import PaymentMethodSetup
from .serializers import (
    PasswordSerializer,
    PaymentMethodSerializer,
    SetupConfirmSerializer,
    SetupIntentRequestSerializer,
)
from .services import PaymentMethodError

logger = logging.getLogger("apps.payment_methods")

# SetupIntent / PaymentMethod events are a few KB.
STRIPE_WEBHOOK_MAX_BYTES = 256 * 1024


def _error(exc: PaymentMethodError):
    return Response({"code": exc.code, "message": exc.message}, status=exc.status)


def _disabled():
    return Response(
        {"code": "SAVED_CARDS_UNAVAILABLE", "message": "Saved cards aren't available yet."},
        status=status.HTTP_404_NOT_FOUND,
    )


class SetupThrottle(UserRateThrottle):
    """Card-testing protection: saving a card without paying is a favourite target."""

    scope = "payment_method_setup"
    rate = "5/hour"


class ChangeThrottle(UserRateThrottle):
    """Remove / set default, which also limits password guesses."""

    scope = "payment_method_change"
    rate = "20/hour"


class PaymentMethodListView(APIView):
    """GET /api/v1/payment-methods/

    → { enabled, limit, can_add, password_required, payment_methods: [...],
        consent: {version, text} }
    The default card comes first. `consent` is the exact wording to show
    next to the "save this card" box, and the version to send back.
    `password_required` is false for social-login accounts: they send no
    password to remove / set default (a recent login is checked instead).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not services.enabled():
            return Response({"enabled": False, "limit": 0, "can_add": False, "payment_methods": []})
        methods = list(services.active_methods(request.user))
        return Response({
            "enabled": True,
            "limit": services.max_methods(),
            "can_add": len(methods) < services.max_methods(),
            "password_required": request.user.has_usable_password(),
            "payment_methods": PaymentMethodSerializer(methods, many=True).data,
            "consent": {"version": services.SAVE_CARD_CONSENT_VERSION, "text": services.SAVE_CARD_CONSENT_TEXT},
        })


class SetupIntentView(APIView):
    """POST /api/v1/payment-methods/setup-intent/  { consent: true, consent_version }

    → 201 { client_secret, publishable_key }. Saves a card without a charge:
    the browser shows Stripe's card form and calls stripe.confirmSetup(),
    then /setup-intent/confirm/.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [SetupThrottle]

    def post(self, request):
        if not services.enabled():
            return _disabled()
        s = SetupIntentRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            client_secret = services.start_setup(
                request.user,
                consent_version=s.validated_data["consent_version"],
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except PaymentMethodError as exc:
            return _error(exc)
        return Response(
            {"client_secret": client_secret, "publishable_key": getattr(settings, "STRIPE_PUBLISHABLE_KEY", "") or ""},
            status=status.HTTP_201_CREATED,
        )


# Customer wording per decision note §5.1.
_SETUP_OUTCOMES = {
    PaymentMethodSetup.Status.DUPLICATE: (
        "DUPLICATE_CARD", "This card is already saved. No payment has been taken.", 409,
    ),
    PaymentMethodSetup.Status.LIMIT_REACHED: (
        "LIMIT_REACHED", "You already have the maximum number of saved cards. No payment has been taken.", 409,
    ),
    PaymentMethodSetup.Status.FAILED: (
        "SETUP_FAILED",
        "We couldn't save your card. No payment has been taken. Please try again or use another card.",
        400,
    ),
}


class SetupConfirmView(APIView):
    """POST /api/v1/payment-methods/setup-intent/confirm/  { setup_intent_id }

    Called after stripe.confirmSetup() succeeds. Checks with Stripe, then
    saves the card. Safe to call more than once.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not services.enabled():
            return _disabled()
        s = SetupConfirmSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            setup = services.confirm_setup(request.user, s.validated_data["setup_intent_id"])
        except PaymentMethodError as exc:
            return _error(exc)

        if setup.status == PaymentMethodSetup.Status.SAVED:
            return Response({
                "status": "saved",
                "message": "Card added. No payment has been taken.",
                "payment_method": PaymentMethodSerializer(setup.payment_method).data,
            })
        code, message, http_status = _SETUP_OUTCOMES[setup.status]
        return Response({"code": code, "message": message}, status=http_status)


class SetDefaultView(APIView):
    """POST /api/v1/payment-methods/<id>/default/  { password }"""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ChangeThrottle]

    def post(self, request, method_id):
        if not services.enabled():
            return _disabled()
        s = PasswordSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            services.confirm_identity(request.user, s.validated_data["password"])
            method = services.set_default(request.user, method_id)
        except PaymentMethodError as exc:
            return _error(exc)
        return Response(PaymentMethodSerializer(method).data)


class RemoveView(APIView):
    """POST /api/v1/payment-methods/<id>/remove/  { password }

    → { removed, new_default }. If the default card is removed, the newest
    remaining card becomes the default (new_default says which).
    POST rather than DELETE so the password can travel in the body.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ChangeThrottle]

    def post(self, request, method_id):
        if not services.enabled():
            return _disabled()
        s = PasswordSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            services.confirm_identity(request.user, s.validated_data["password"])
            method = services.remove(request.user, method_id)
        except PaymentMethodError as exc:
            return _error(exc)
        new_default = services.active_methods(request.user).filter(is_default=True).first()
        return Response({
            "removed": str(method.pk),
            "new_default": PaymentMethodSerializer(new_default).data if new_default else None,
        })


@csrf_exempt  # nosemgrep: no-csrf-exempt -- Stripe webhook, verified via signature, not CSRF token
def stripe_webhook(request):
    """POST /api/v1/payment-methods/webhook/

    Subscribed events: setup_intent.succeeded, payment_method.detached,
    payment_method.updated, payment_method.automatically_updated.
    Payments themselves stay on the checkout and add-line webhooks.
    A 500 is returned only when a save failed and a retry could fix it.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    secret = usable_secret(getattr(settings, "STRIPE_PAYMENT_METHODS_WEBHOOK_SECRET", ""))
    if not secret:
        logger.error("Payment-methods webhook rejected: STRIPE_PAYMENT_METHODS_WEBHOOK_SECRET not set or a placeholder")
        return HttpResponse("Service unavailable", status=503)

    problem = request_problem(request, STRIPE_WEBHOOK_MAX_BYTES)
    if problem:
        return JsonResponse({"error": problem[1]}, status=problem[0])

    try:
        event = stripe.Webhook.construct_event(request.body, request.META.get("HTTP_STRIPE_SIGNATURE", ""), secret)
    except ValueError:
        return HttpResponse("Invalid payload", status=400)
    except stripe.error.SignatureVerificationError:
        return HttpResponse("Invalid signature", status=400)

    event = to_plain(event)
    if event.get("livemode") is not services.expected_livemode():
        logger.error("Payment-methods webhook %s ignored: livemode does not match this environment", event.get("id"))
        return JsonResponse({"received": True, "ignored": "wrong mode"})
    if not services.enabled():
        return JsonResponse({"received": True, "ignored": "feature disabled"})

    etype = event.get("type")
    obj = event["data"]["object"]
    try:
        if etype == "setup_intent.succeeded":
            if (obj.get("metadata") or {}).get("source") != services.STRIPE_SOURCE:
                return JsonResponse({"received": True, "ignored": "not a saved-card setup"})
            services.finish_setup(obj)
        elif etype == "payment_method.detached":
            services.mark_removed(obj.get("id"), reason="detached_in_stripe")
        elif etype in ("payment_method.updated", "payment_method.automatically_updated"):
            services.apply_card_update(obj)
    except PaymentMethodError as exc:
        # Unknown or mismatched setup: a retry can't fix it.
        logger.warning("Payment-methods webhook %s (%s) not applied: %s", event.get("id"), etype, exc.code)
        return JsonResponse({"received": True, "ignored": exc.code})
    except Exception:
        logger.exception("Payment-methods webhook %s (%s) failed; asking Stripe to retry", event.get("id"), etype)
        return HttpResponse("Processing failed", status=500)

    return JsonResponse({"received": True})
