import logging

import stripe
from django.conf import settings
from django.db.models import Prefetch
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from core.stripe_compat import to_plain

from .models import AddLineOrder, Line, LineClaim, PortRequest
from .serializers import (
    AddLineOrderSerializer,
    CheckoutRequestSerializer,
    ClaimRequestSerializer,
    EligibilitySerializer,
    LineClaimSerializer,
    LineSerializer,
    PortCodeSerializer,
    PortRequestCreateSerializer,
    PortRequestSerializer,
    QuoteRequestSerializer,
)
from .services import capacity, checkout, claims, eligibility, pac, payments, porting
from .services.quote import QuoteError, build_quote, issue_token
from .services.secrets import request_problem, usable_secret

logger = logging.getLogger("apps.add_line")

# PaymentIntent events are a few KB; anything far larger isn't from Stripe.
STRIPE_WEBHOOK_MAX_BYTES = 256 * 1024


def _error(code, message, http_status):
    return Response({"code": code, "message": message}, status=http_status)


class LinesView(APIView):
    """GET /api/v1/lines/  → the customer's lines plus orders not yet a line.

    Replaces the dashboard's direct BeQuick calls. Cancelled lines are left
    out; terminated ones stay visible so the customer sees their history.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        lines = (
            Line.objects.filter(owner=request.user)
            .exclude(status=Line.Status.CANCELLED)
            .select_related("plan", "sim", "order")
            .prefetch_related(Prefetch("port_requests", queryset=PortRequest.objects.order_by("-created_at")))
        )
        pending_orders = AddLineOrder.objects.filter(
            owner=request.user, line__isnull=True,
            payment_status__in=[AddLineOrder.PaymentStatus.PENDING, AddLineOrder.PaymentStatus.PAID],
        ).exclude(fulfilment_status=AddLineOrder.FulfilmentStatus.CANCELLED)
        usage = capacity.get_usage(request.user)
        return Response({
            "lines": LineSerializer(lines, many=True).data,
            "pending_orders": AddLineOrderSerializer(pending_orders, many=True).data,
            "capacity": {"used": usage.used, "limit": usage.limit, "remaining": usage.remaining},
        })


class ClaimThrottle(UserRateThrottle):
    scope = "add_line_claim"
    rate = "10/hour"


class ClaimsView(APIView):
    """GET  /api/v1/lines/claims/  → the customer's existing-SIM claims
    POST /api/v1/lines/claims/  { iccid?, msisdn?, order_reference? } (any two)

    Auto-links the SIM when the evidence is strong, otherwise queues it for
    support. The reply is the same whether or not anything matched.
    """

    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        return [ClaimThrottle()] if self.request.method == "POST" else []

    def get(self, request):
        return Response(LineClaimSerializer(LineClaim.objects.filter(owner=request.user), many=True).data)

    def post(self, request):
        s = ClaimRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            claim = claims.submit_claim(request.user, **s.validated_data)
        except claims.ClaimError as exc:
            return _error(exc.code, exc.message, exc.status)
        return Response(LineClaimSerializer(claim).data, status=status.HTTP_201_CREATED)


class EligibilityView(APIView):
    """GET /api/v1/lines/eligibility/  → can the logged-in customer add a line?

    The customer is always taken from the token (account holder only, Q3).
    `lines.in_progress` counts unexpired checkout holds and paid orders still
    awaiting activation, so the frontend can show "x of 5 used".
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        result = eligibility.check(request.user)
        return Response(EligibilitySerializer(result).data)


class QuoteView(APIView):
    """POST /api/v1/lines/quote/  { plan_id, term, sim_type, promo_code? }

    Returns the VAT-inclusive price breakdown plus a signed `quote_token` that
    checkout must send back; the price is never taken from the browser.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        s = QuoteRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            quote = build_quote(request.user, **s.validated_data)
        except QuoteError as exc:
            return _error(exc.code, exc.message, status.HTTP_400_BAD_REQUEST)
        return Response({
            "quote": quote.as_dict(),
            "quote_token": issue_token(request.user, quote),
            "expires_in_seconds": settings.ADD_LINE_QUOTE_MINUTES * 60,
            # Versions the customer accepts at checkout; sent back unchanged.
            "contract": {
                "contract_version": settings.ADD_LINE_CONTRACT_VERSION,
                "contract_summary_version": settings.ADD_LINE_CONTRACT_SUMMARY_VERSION,
            },
        })


class CheckoutView(APIView):
    """POST /api/v1/lines/checkout/

    { quote_token, switch_choice, billing, delivery_address?, contract }
      → { order_reference, client_secret, payment_required }

    Holds a line slot and a SIM, records the contract acceptance and creates
    the Stripe PaymentIntent. The browser confirms the payment with
    `client_secret`, then calls /orders/<ref>/confirm/ (the Stripe webhook
    does the same independently).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        s = CheckoutRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data
        try:
            order, client_secret = checkout.start_checkout(
                request.user,
                quote_token=data["quote_token"],
                switch_choice=data["switch_choice"],
                billing=data["billing"],
                delivery_address=data["delivery_address"],
                contract=data["contract"],
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except checkout.CheckoutError as exc:
            return _error(exc.code, exc.message, exc.status)

        if client_secret is None:
            order = payments.mark_free_order_paid(order)

        return Response({
            "order_reference": order.order_reference,
            "client_secret": client_secret,
            "payment_required": client_secret is not None,
            "amount": str(order.total_gross),
            "currency": order.currency,
        }, status=status.HTTP_201_CREATED)


class OrderDetailView(APIView):
    """GET /api/v1/lines/orders/<ref>/  → status of one of the customer's add-line orders."""

    permission_classes = [IsAuthenticated]

    def get(self, request, order_reference):
        order = get_object_or_404(
            AddLineOrder.objects.select_related("line"),
            order_reference=order_reference, owner=request.user,
        )
        return Response(AddLineOrderSerializer(order).data)


class ConfirmPaymentView(APIView):
    """POST /api/v1/lines/orders/<ref>/confirm/

    Called by the browser after stripe.confirmPayment() succeeds. Asks Stripe
    directly whether the PaymentIntent succeeded — the browser's word is never
    trusted — then marks the order paid and starts activation.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, order_reference):
        order = get_object_or_404(AddLineOrder, order_reference=order_reference, owner=request.user)

        if order.payment_status != AddLineOrder.PaymentStatus.PAID:
            if not order.stripe_payment_intent_id or not checkout.stripe_configured():
                return _error("PAYMENTS_UNAVAILABLE", "Payment service not configured.", 503)
            try:
                intent = to_plain(stripe.PaymentIntent.retrieve(order.stripe_payment_intent_id))
            except Exception:
                logger.exception("Could not retrieve PaymentIntent for %s", order.order_reference)
                return _error("PAYMENT_CHECK_FAILED", "We couldn't check your payment yet.", 502)
            if intent.get("status") != "succeeded":
                if intent.get("last_payment_error"):
                    payments.record_payment_failure(order, intent)
                return _error("PAYMENT_NOT_COMPLETED", "Payment not completed yet.", 402)
            try:
                order = payments.handle_payment_succeeded(order, intent)
            except payments.PaymentMismatch:
                logger.exception("PaymentIntent mismatch on confirm")
                return _error("PAYMENT_MISMATCH", "We couldn't match this payment. Please contact support.", 409)

        order.refresh_from_db()
        return Response(AddLineOrderSerializer(order).data)


class PacCheckView(APIView):
    """POST /api/v1/lines/pac/check/  { code_type, code, msisdn }

    Format check only — usable before payment. Transatel validates the code
    with the old network after it is submitted, so this never says "verified".
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        s = PortCodeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            pac.normalise_code(s.validated_data["code_type"], s.validated_data["code"])
            msisdn = pac.normalise_msisdn(s.validated_data["msisdn"])
        except pac.PacError as exc:
            return _error(exc.code, exc.message, status.HTTP_400_BAD_REQUEST)
        return Response({
            "format_ok": True,
            "msisdn": msisdn,
            "message": "Format checked. Your previous provider confirms the code after you submit it.",
        })


class LinePortView(APIView):
    """GET  /api/v1/lines/<line_id>/port/  → {"port_request": latest transfer or null}
    POST /api/v1/lines/<line_id>/port/  { code_type, code, msisdn, wished_date? }

    The code can be given right after checkout or later. It is held
    encrypted and only sent to Transatel once the line is active.
    """

    permission_classes = [IsAuthenticated]

    def _line(self, request, line_id):
        return get_object_or_404(Line, uuid=line_id, owner=request.user)

    def get(self, request, line_id):
        port = self._line(request, line_id).port_requests.order_by("-created_at").first()
        return Response({"port_request": PortRequestSerializer(port).data if port else None})

    def post(self, request, line_id):
        line = self._line(request, line_id)
        s = PortRequestCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            port = porting.create_request(request.user, line, **s.validated_data)
        except porting.PortError as exc:
            return _error(exc.code, exc.message, exc.status)
        return Response(PortRequestSerializer(port).data, status=status.HTTP_201_CREATED)


@csrf_exempt  # nosemgrep: no-csrf-exempt -- Stripe webhook, verified via signature, not CSRF token
def stripe_webhook(request):
    """POST /api/v1/lines/webhooks/stripe/

    Source of truth for "paid" on add-line orders. Only PaymentIntents created
    by add-line checkout (metadata.source) are handled; everything else is
    acknowledged and ignored. A 500 is returned only when the paid state could
    not be saved, so Stripe retries; retrying can't fix a mismatch, so those
    are acknowledged and logged.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    secret = usable_secret(settings.STRIPE_ADD_LINE_WEBHOOK_SECRET)
    if not secret:
        logger.error("Add-line Stripe webhook rejected: STRIPE_ADD_LINE_WEBHOOK_SECRET not set or a placeholder")
        return HttpResponse("Service unavailable", status=503)

    problem = request_problem(request, STRIPE_WEBHOOK_MAX_BYTES)
    if problem:
        return JsonResponse({"error": problem[1]}, status=problem[0])

    try:
        event = stripe.Webhook.construct_event(
            request.body, request.META.get("HTTP_STRIPE_SIGNATURE", ""), secret
        )
    except ValueError:
        return HttpResponse("Invalid payload", status=400)
    except stripe.error.SignatureVerificationError:
        return HttpResponse("Invalid signature", status=400)

    event = to_plain(event)
    if event.get("livemode") is not payments.expected_livemode():
        logger.error("Add-line webhook %s ignored: livemode does not match this environment", event.get("id"))
        return JsonResponse({"received": True, "ignored": "wrong mode"})

    intent = event["data"]["object"]
    metadata = intent.get("metadata") or {}
    if metadata.get("source") != checkout.STRIPE_SOURCE:
        return JsonResponse({"received": True, "ignored": "not an add-line payment"})

    order = AddLineOrder.objects.filter(order_reference=metadata.get("add_line_order")).first()
    if not order:
        logger.error("Add-line webhook for unknown order %s", metadata.get("add_line_order"))
        return JsonResponse({"received": True, "ignored": "unknown order"})

    try:
        if event["type"] == "payment_intent.succeeded":
            # Fulfilment errors are absorbed inside; anything raised here means
            # the paid state was not saved.
            payments.handle_payment_succeeded(order, intent)
        elif event["type"] == "payment_intent.payment_failed":
            payments.record_payment_failure(order, intent)
        elif event["type"] == "payment_intent.canceled":
            if order.payment_status == AddLineOrder.PaymentStatus.PENDING:
                checkout.release(order, "payment_canceled")
    except payments.PaymentMismatch:
        logger.exception("PaymentIntent mismatch in webhook")
        return JsonResponse({"received": True, "ignored": "mismatch"})
    except Exception:
        logger.exception("Add-line webhook %s failed for %s; asking Stripe to retry",
                         event["type"], order.order_reference)
        return HttpResponse("Processing failed", status=500)

    return JsonResponse({"received": True})
