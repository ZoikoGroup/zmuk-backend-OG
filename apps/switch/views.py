import logging
from hmac import compare_digest

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.add_line.services.quote import QuoteError, build_quote

from . import services
from .models import SwitchFeedback, SwitchRequest
from .serializers import (
    FeedbackSerializer,
    PortCodeSerializer,
    PublicQuoteSerializer,
    SwitchRequestSerializer,
    SwitchStatusSerializer,
)

logger = logging.getLogger("apps.switch")

NOT_FOUND_MESSAGE = "We couldn't find a switch request with that reference and email."


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


def _ok(message, data, http_status=status.HTTP_200_OK):
    return Response({"success": True, "message": message, "data": data}, status=http_status)


def _fail(message, http_status, code):
    """Non-field error. Field errors use DRF's {field: ["msg"]} instead."""
    return Response({"success": False, "message": message, "code": code}, status=http_status)


# ── Throttles (per IP, so they also apply to logged-in users) ─────────────────

class _IPThrottle(SimpleRateThrottle):
    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class SwitchSubmitThrottle(_IPThrottle):
    scope = "switch_submit"
    rate = "20/hour"


class SwitchLookupThrottle(_IPThrottle):
    # Step 5 polls this; ~ every 30 s for an hour.
    scope = "switch_lookup"
    rate = "120/hour"


class SwitchCodeThrottle(_IPThrottle):
    scope = "switch_code"
    rate = "10/hour"


class SwitchFeedbackThrottle(_IPThrottle):
    scope = "switch_feedback"
    rate = "10/hour"


class SwitchQuoteThrottle(_IPThrottle):
    scope = "switch_quote"
    rate = "60/hour"


# ── Access helper ─────────────────────────────────────────────────────────────

def _find_for_requester(request, reference, email):
    """The request if the caller owns it (token) or gave its email; else None.

    The same None is returned for "no such reference" and "wrong email" so
    the endpoints can't be used to discover references.
    """
    switch = (
        SwitchRequest.objects.select_related("plan", "order", "line")
        .filter(reference=(reference or "").strip().upper())
        .first()
    )
    if not switch:
        return None
    if request.user.is_authenticated and switch.user_id == request.user.id:
        return switch
    given = (email or "").strip().lower()
    if given and compare_digest(given.encode(), switch.email.strip().lower().encode()):
        return switch
    return None


# ── Views ─────────────────────────────────────────────────────────────────────

class SwitchRequestCreateView(generics.CreateAPIView):
    """POST /api/switch-requests/  — public; links the user when a token is sent."""

    queryset = SwitchRequest.objects.all()
    serializer_class = SwitchRequestSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [SwitchSubmitThrottle]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            instance = serializer.save()
        except services.CodeStorageUnavailable:
            logger.error("PAC_ENCRYPTION_KEY missing/invalid; switch code not accepted")
            return _fail(
                "We can't accept switching codes online right now. "
                "Please choose to provide your code later.",
                status.HTTP_503_SERVICE_UNAVAILABLE, code="CODES_UNAVAILABLE",
            )

        ctx = {
            "reference":              instance.reference,
            "first_name":             instance.first_name,
            "last_name":              instance.last_name,
            "email":                  instance.email,
            "mobile":                 instance.mobile,
            "postcode":               instance.postcode,
            "current_provider":       instance.current_provider or "",
            "current_plan_cost":      instance.current_plan_cost,
            "current_data_allowance": instance.current_data_allowance,
            "selected_plan":          instance.selected_plan,
            "contract_term":          instance.get_contract_term_display(),
            "sim_type":               instance.get_sim_type_display(),
            "switch_route":           instance.get_switch_route_display(),
            "code_status":            services.pac_status(instance),
            "promo_code":             instance.promo_code,
            "created_at":             instance.created_at.strftime("%d %b %Y, %H:%M UTC"),
        }

        # 1) Confirmation email → customer
        _send_html_email(
            subject=f"Your Zoiko Mobile switch request {instance.reference} is confirmed",
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
            subject=(
                f"New Switch Request {instance.reference} — "
                f"{instance.first_name} {instance.last_name} → {instance.selected_plan}"
            ),
            template_name="emails/switch_request_ops.html",
            context=ctx,
            recipient=ops_email,
        )

        return _ok(
            "Your switch request has been received. We'll be in touch within 1 working day.",
            self.get_serializer(instance).data,
            status.HTTP_201_CREATED,
        )


class MySwitchRequestsView(APIView):
    """GET /api/switch-requests/mine/  — the logged-in customer's requests."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = SwitchRequest.objects.filter(user=request.user).select_related("plan", "order", "line")
        return _ok("Your switch requests.", SwitchStatusSerializer(qs, many=True).data)


class SwitchStatusByReferenceView(APIView):
    """GET /api/switch-requests/by-reference/<reference>/?email=<email>

    Guests give the email used on the form; the owner can omit it.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [SwitchLookupThrottle]

    def get(self, request, reference):
        switch = _find_for_requester(request, reference, request.query_params.get("email"))
        if not switch:
            return _fail(NOT_FOUND_MESSAGE, status.HTTP_404_NOT_FOUND, code="NOT_FOUND")
        return _ok("Switch request status.", SwitchStatusSerializer(switch).data)


class SwitchPortCodeView(APIView):
    """POST /api/switch-requests/<reference>/port-code/
    { code_type: "PAC"|"STAC", code, msisdn?, email }

    Stores the code encrypted. It is sent to Transatel only once the order
    and line exist and the line is active, so nothing is ported here.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [SwitchCodeThrottle]

    def post(self, request, reference):
        s = PortCodeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        switch = _find_for_requester(request, reference, data.get("email"))
        if not switch:
            return _fail(NOT_FOUND_MESSAGE, status.HTTP_404_NOT_FOUND, code="NOT_FOUND")

        if switch.status in SwitchRequest.CLOSED_STATUSES:
            return _fail("This switch request is closed. Please contact support.",
                         status.HTTP_409_CONFLICT, code="REQUEST_CLOSED")
        if switch.line_id and switch.line.port_requests.exists():
            return _fail("Your number transfer has already started. Please contact support to change the code.",
                         status.HTTP_409_CONFLICT, code="TRANSFER_STARTED")

        needed = switch.required_code_type
        if needed is None:
            raise ValidationError({"code_type": ["No code is needed for this switch option."]})
        if data["code_type"] != needed:
            raise ValidationError(
                {"code_type": [f"Your switch option needs a {needed}, not a {data['code_type']}."]}
            )

        errors = {}
        try:
            code = services.normalise_code(needed, data["code"])
        except services.PacError as exc:
            errors["code"] = [exc.message]
        msisdn = ""
        number = data.get("msisdn") or ("" if switch.port_number else switch.mobile)
        if number:
            try:
                msisdn = services.normalise_msisdn(number)
            except services.PacError as exc:
                errors["msisdn"] = [exc.message]
        if errors:
            raise ValidationError(errors)

        try:
            services.apply_code(switch, needed, code, msisdn)
        except services.CodeStorageUnavailable:
            logger.error("PAC_ENCRYPTION_KEY missing/invalid; switch code not accepted")
            return _fail("We can't accept switching codes online right now. Please try again later.",
                         status.HTTP_503_SERVICE_UNAVAILABLE, code="CODES_UNAVAILABLE")
        switch.save(update_fields=[
            "encrypted_code", "code_type", "code_received_at", "code_purged_at",
            "pac_later", "port_number", "updated_at",
        ])
        services.record_code_received(switch, actor=request.user)

        return _ok(
            f"Thanks, we've received your {needed}. Your previous provider confirms it "
            "when we start the transfer.",
            SwitchStatusSerializer(switch).data,
        )


class SwitchFeedbackView(APIView):
    """POST /api/switch-requests/<reference>/feedback/  { rating 1-5, comments?, email }"""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [SwitchFeedbackThrottle]

    def post(self, request, reference):
        s = FeedbackSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        email = s.validated_data.pop("email", "")

        switch = _find_for_requester(request, reference, email)
        if not switch:
            return _fail(NOT_FOUND_MESSAGE, status.HTTP_404_NOT_FOUND, code="NOT_FOUND")

        try:
            with transaction.atomic():
                feedback = SwitchFeedback.objects.create(switch_request=switch, **s.validated_data)
        except IntegrityError:
            return _fail("You've already sent feedback for this switch. Thank you!",
                         status.HTTP_409_CONFLICT, code="FEEDBACK_EXISTS")
        return _ok("Thanks for your feedback!", FeedbackSerializer(feedback).data,
                   status.HTTP_201_CREATED)


class SwitchQuoteView(APIView):
    """POST /api/v1/switch/quote/  { plan_id, term, sim_type, promo_code? }

    Public price preview. Same `quote` shape as /api/v1/lines/quote/, but no
    quote_token: nothing is paid at submit, so there is nothing to redeem.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [SwitchQuoteThrottle]

    def post(self, request):
        s = PublicQuoteSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data
        user = request.user if request.user.is_authenticated else None
        try:
            quote = build_quote(user, data["plan_id"], data["term"], data["sim_type"],
                                data["promo_code"], additional_line=False)
        except QuoteError as exc:
            field = "promo_code" if exc.code.startswith("PROMO_") else (
                "plan_id" if exc.code.startswith("PLAN_") else None)
            body = {"code": exc.code, "message": exc.message}
            if field:
                body["field"] = field
                body[field] = [exc.message]
            return Response(body, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "quote": quote.as_dict(),
            "quote_token": None,
            "expires_in_seconds": None,
            "contract": {
                "contract_version": settings.ADD_LINE_CONTRACT_VERSION,
                "contract_summary_version": settings.ADD_LINE_CONTRACT_SUMMARY_VERSION,
            },
        })


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
