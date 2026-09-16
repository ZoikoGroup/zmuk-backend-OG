import logging

from django.conf import settings
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework import status

from apps.sims.models import Sim
from apps.sims.transatel.client import APIClient as TransatelClient
from apps.sims.transatel.exceptions import TransatelAPIError

from .models import RechargeModule, RechargeOrder, RechargeProduct, TransatelLog
from .serializers import (
    RechargeModuleSerializer,
    RechargeOrderSerializer,
    RechargeProductSerializer,
    CreateRechargeSerializer,
    CreatePaymentIntentSerializer,
    ConfirmPaymentSerializer,
    PhoneValidateSerializer,
    SimDetailSerializer,
    mask_identifier,
    phone_variants,
)
from . import services
from . import reactivation as reactivation_service

logger = logging.getLogger("apps.recharge")


# ── Phone Validation ─────────────────────────────────────────────────────

class ValidatePhoneView(APIView):
    """POST /api/recharge/validate-phone/

    The complete flow:
      1. Phone number → local DB → find ICCID
      2. ICCID → LIVE Transatel API call → get real current status
      3. Based on real status → decide rechargeable or not
      4. Log everything in TransatelLog

    Request:  { "phone_number": "+447421118918" }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PhoneValidateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        phone = serializer.validated_data["phone_number"]

        # ── Step 1: Phone → local DB → ICCID ────────────────────────────
        variants = phone_variants(phone, country_code="44")
        sim = Sim.objects.filter(msisdn__in=variants).first()

        if not sim:
            return Response(
                {"success": False, "message": "No SIM found for this phone number."},
                status=status.HTTP_404_NOT_FOUND,
            )

        iccid = sim.serial_number or sim.iccid
        if not iccid:
            return Response(
                {"success": False, "message": "SIM found but has no ICCID stored."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # ── Step 2: ICCID → LIVE Transatel API → real status ────────────
        live_status = None
        live_data = None
        transatel_error = None
        transatel_http_status = None

        try:
            client = TransatelClient()
            endpoint = (
                f"/connectivity-management/subscribers/api/subscribers"
                f"/sim-serial/{iccid}"
            )
            result = client.get(endpoint)
            transatel_http_status = result.get("status_code", 0)
            live_data = result.get("data", {})

            if result.get("success"):
                # Extract status from the live response.
                # Transatel may use different keys — try the known ones.
                live_status = (
                    live_data.get("simStatus")
                    or live_data.get("status")
                    or live_data.get("subscriberStatus")
                    or live_data.get("provisioningStatus")
                    or ""
                ).strip()
            else:
                transatel_error = live_data.get("detail") or live_data.get(
                    "title"
                ) or f"HTTP {transatel_http_status}"

        except TransatelAPIError as exc:
            transatel_error = str(exc)
            logger.warning(
                "Transatel live lookup failed for %s: %s", iccid, exc
            )
        except Exception as exc:
            transatel_error = str(exc)
            logger.exception(
                "Unexpected error during Transatel lookup for %s", iccid
            )

        # ── Step 3: Decide rechargeable based on LIVE status ─────────────
        # Use live status if we got it, fall back to local DB status.
        if live_status:
            sim_status = live_status
            status_source = "transatel_live"
        else:
            sim_status = (sim.provisioning_status or "").strip() or "Unknown"
            status_source = "local_db"

        is_suspended = sim_status.lower() == "suspended"

        # If live status differs from local, update local DB to stay in sync.
        if live_status and live_status != (sim.provisioning_status or ""):
            sim.provisioning_status = live_status
            sim.save(update_fields=["provisioning_status"])

        rechargeable = True
        message = "Phone number validated successfully!"

        # Optional hard block (set RECHARGE_BLOCK_NON_SUSPENDED = True)
        block_non_suspended = getattr(
            settings, "RECHARGE_BLOCK_NON_SUSPENDED", False
        )
        if block_non_suspended and not is_suspended:
            rechargeable = False
            message = (
                f"This SIM is currently {sim_status}. "
                f"Recharge is available for suspended SIMs."
            )

        sim_data = {
            "phone_number": sim.msisdn or phone,
            "sim_card_id_masked": mask_identifier(sim.serial_number or sim.iccid),
            "sim_iccid_masked": mask_identifier(sim.iccid) if sim.iccid else "",
            "sim_status": sim_status,
            "rechargeable": rechargeable,
            "is_suspended": is_suspended,
        }

        # ── Step 4: Log everything ───────────────────────────────────────
        TransatelLog.objects.create(
            action="validate_phone",
            msisdn_masked=mask_identifier(phone),
            sim_serial_masked=mask_identifier(iccid),
            success=live_status is not None,
            response_body={
                "status_source": status_source,
                "live_status": live_status,
                "local_status": sim.provisioning_status,
                "rechargeable": rechargeable,
                "transatel_error": transatel_error,
                "transatel_http_status": transatel_http_status,
            },
        )

        return Response({
            "success": True,
            "message": message,
            "sim": SimDetailSerializer(sim_data).data,
            "sim_serial": sim.serial_number or sim.iccid,
            "sim_iccid": sim.iccid or "",
            "status_source": status_source,
            "transatel_live": {
                "checked": live_status is not None,
                "status": live_status,
                "error": transatel_error,
                "raw": live_data if live_data and live_status else None,
            },
        })


# ── Recharge Products / Plans ────────────────────────────────────────────

class RechargeProductsView(APIView):
    """GET /api/recharge/products/?module=recharge

    Returns the active plans for a given module. This is what populates
    the "Select Recharge Plan" modal.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        module = request.query_params.get("module", "recharge")
        products = RechargeProduct.objects.filter(
            module=module, is_active=True
        )
        return Response({
            "success": True,
            "products": RechargeProductSerializer(products, many=True).data,
        })


# ── Modules ──────────────────────────────────────────────────────────────

class RechargeModulesView(APIView):
    """GET /api/recharge/modules/"""
    permission_classes = [AllowAny]

    def get(self, request):
        mods = RechargeModule.objects.all()
        return Response(RechargeModuleSerializer(mods, many=True).data)


# ── Stats ────────────────────────────────────────────────────────────────

class RechargeStatsView(APIView):
    """GET /api/recharge/stats/"""
    permission_classes = [AllowAny]

    def get(self, request):
        completed = RechargeOrder.objects.filter(status=RechargeOrder.STATUS_COMPLETED)
        today = timezone.now().date()
        today_qs = completed.filter(created_at__date=today)

        total_amount = completed.aggregate(s=Sum("amount_pence"))["s"] or 0
        today_amount = today_qs.aggregate(s=Sum("amount_pence"))["s"] or 0

        return Response({
            "total_recharges": completed.count(),
            "total_amount": f"£{total_amount / 100:.2f}",
            "today_recharges": today_qs.count(),
            "today_amount": f"£{today_amount / 100:.2f}",
        })


# ── Orders List ──────────────────────────────────────────────────────────

class RechargeOrdersView(APIView):
    """GET /api/recharge/orders/"""
    permission_classes = [AllowAny]

    def get(self, request):
        limit = int(request.query_params.get("limit", 20))
        orders = RechargeOrder.objects.all()[:limit]
        return Response(RechargeOrderSerializer(orders, many=True).data)


# ── Order Detail ─────────────────────────────────────────────────────────

class RechargeOrderDetailView(APIView):
    """GET /api/recharge/orders/<order_ref>/"""
    permission_classes = [AllowAny]

    def get(self, request, order_ref):
        order = RechargeOrder.objects.filter(order_ref=order_ref).first()
        if not order:
            return Response(
                {"success": False, "message": "Order not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        data = RechargeOrderSerializer(order).data

        # Include reactivation status
        attempt = order.reactivation_attempts.order_by("-updated_at").first()
        data["reactivation_status"] = attempt.status if attempt else None
        data["reactivation_transaction_id"] = attempt.provider_transaction_id if attempt else None

        return Response({"success": True, "order": data})


# ── Create Order ─────────────────────────────────────────────────────────

class CreateRechargeView(APIView):
    """POST /api/recharge/create/

    Creates order + Stripe Checkout Session. Returns {order_ref, checkout_url}.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = CreateRechargeSerializer(data=request.data, context={})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        amount_pence = data["_amount_pence"]
        product = data.get("_product")

        order = RechargeOrder.objects.create(
            module=data["module"],
            msisdn=data["msisdn"],
            sim_serial=data.get("sim_serial", "") or "",
            sim_iccid=data.get("sim_iccid", "") or "",
            customer_name=data.get("customer_name", "") or "",
            customer_email=data.get("customer_email", "") or "",
            product=product,
            amount_pence=amount_pence,
            currency="gbp",
            status=RechargeOrder.STATUS_PENDING,
        )

        try:
            session = services.create_checkout_session(
                order,
                success_url=data["success_url"],
                cancel_url=data["cancel_url"],
            )
        except services.StripeNotConfigured as exc:
            order.status = RechargeOrder.STATUS_FAILED
            order.save(update_fields=["status"])
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception as exc:
            order.status = RechargeOrder.STATUS_FAILED
            order.save(update_fields=["status"])
            logger.exception("Stripe session creation failed for %s", order.order_ref)
            return Response(
                {"detail": f"Payment could not be started: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        order.stripe_session_id = session.id
        order.save(update_fields=["stripe_session_id"])

        return Response(
            {"order_ref": order.order_ref, "checkout_url": session.url},
            status=status.HTTP_201_CREATED,
        )


# ── Inline Payment: Create PaymentIntent ─────────────────────────────────

class CreatePaymentIntentView(APIView):
    """POST /api/recharge/create-intent/

    Creates the order and a Stripe PaymentIntent for INLINE payment — the
    browser never leaves the page. This mirrors the WordPress modal flow
    (plan grid -> Google Pay / Card -> Pay Now -> Success), instead of
    redirecting to Stripe's hosted Checkout page.

    Request:
        {
          "msisdn": "+447421118918",
          "module": "recharge",
          "product_id": 1,
          "sim_serial": "8944122666...",
          "sim_iccid": "8944122666...",
          "customer_email": "you@example.com"
        }

    Response:
        {
          "order_ref": "RC-XXXXXXXXXX",
          "payment_intent_id": "pi_...",
          "client_secret": "pi_..._secret_...",
          "amount": 1214,
          "amount_display": "\u00a312.14",
          "currency": "gbp",
          "publishable_key": "pk_test_..."
        }

    The browser then calls stripe.confirmPayment({ clientSecret }) and, on
    success, POSTs to /api/recharge/confirm/.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = CreatePaymentIntentSerializer(data=request.data, context={})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        order = RechargeOrder.objects.create(
            module=data["module"],
            msisdn=data["msisdn"],
            sim_serial=data.get("sim_serial", "") or "",
            sim_iccid=data.get("sim_iccid", "") or "",
            customer_name=data.get("customer_name", "") or "",
            customer_email=data.get("customer_email", "") or "",
            product=data.get("_product"),
            amount_pence=data["_amount_pence"],
            currency="gbp",
            status=RechargeOrder.STATUS_PENDING,
        )

        try:
            intent = services.create_payment_intent(
                order, customer_email=data.get("customer_email", "") or ""
            )
        except services.StripeNotConfigured as exc:
            order.status = RechargeOrder.STATUS_FAILED
            order.save(update_fields=["status"])
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception as exc:
            order.status = RechargeOrder.STATUS_FAILED
            order.save(update_fields=["status"])
            logger.exception("PaymentIntent creation failed for %s", order.order_ref)
            return Response(
                {"detail": f"Payment could not be started: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        order.stripe_payment_intent_id = intent.id
        order.save(update_fields=["stripe_payment_intent_id"])

        return Response(
            {
                "success": True,
                "order_ref": order.order_ref,
                "payment_intent_id": intent.id,
                "client_secret": intent.client_secret,
                "amount": order.amount_pence,
                "amount_display": order.amount_display,
                "currency": order.currency,
                "publishable_key": getattr(settings, "STRIPE_PUBLISHABLE_KEY", "") or "",
            },
            status=status.HTTP_201_CREATED,
        )


# ── Inline Payment: Confirm + Reactivate ─────────────────────────────────

class ConfirmPaymentView(APIView):
    """POST /api/recharge/confirm/

    Called by the browser right after Stripe.js reports the payment
    succeeded. The server re-verifies the PaymentIntent with Stripe (never
    trusting the client), marks the order paid, then runs the Transatel
    reactivation SYNCHRONOUSLY so the modal can show "Success! Order Number:
    RC-XXXX" straight away — same as the WordPress flow.

    The Stripe webhook remains the safety net: if the browser closes before
    this call lands, the webhook still completes the order.

    Request:  { "order_ref": "RC-XXXXXXXXXX", "payment_intent_id": "pi_..." }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ConfirmPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order_ref = serializer.validated_data["order_ref"]
        intent_id = serializer.validated_data["payment_intent_id"]

        order = RechargeOrder.objects.filter(order_ref=order_ref).first()
        if not order:
            return Response(
                {"success": False, "message": "Order not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # The intent must belong to this order — stops someone confirming
        # order A with a payment that actually paid for order B.
        if order.stripe_payment_intent_id and order.stripe_payment_intent_id != intent_id:
            return Response(
                {"success": False, "message": "Payment does not match this order."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Already finished (webhook got here first) — return the same shape.
        if order.status == RechargeOrder.STATUS_COMPLETED:
            return Response(self._success_payload(order))

        # ── Verify with Stripe ──
        try:
            intent = services.retrieve_payment_intent(intent_id)
        except services.StripeNotConfigured as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception as exc:
            logger.exception("Could not retrieve PaymentIntent for %s", order_ref)
            return Response(
                {"success": False, "message": f"Could not verify payment: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if intent.status != "succeeded":
            return Response(
                {
                    "success": False,
                    "message": f"Payment not completed (status: {intent.status}).",
                    "payment_status": intent.status,
                },
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        # Amount check — the intent must have charged what we asked for.
        if int(intent.amount) != int(order.amount_pence):
            logger.error(
                "Amount mismatch for %s: intent=%s order=%s",
                order_ref, intent.amount, order.amount_pence,
            )
            return Response(
                {"success": False, "message": "Payment amount does not match the order."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Mark paid ──
        if order.status == RechargeOrder.STATUS_PENDING:
            order.status = RechargeOrder.STATUS_PROCESSING
            order.stripe_payment_intent_id = intent_id
            order.paid_at = timezone.now()
            order.save(update_fields=[
                "status", "stripe_payment_intent_id", "paid_at", "updated_at",
            ])

        # ── Reactivate (idempotent — safe if the webhook also runs) ──
        attempt = None
        if order.sim_serial:
            try:
                attempt = reactivation_service.reactivate_for_order(order)
            except Exception:
                logger.exception("Reactivation error for %s — queued for retry", order_ref)
        else:
            # No SIM attached (e.g. a plain top-up) — nothing to reactivate.
            order.status = RechargeOrder.STATUS_COMPLETED
            order.completed_at = timezone.now()
            order.save(update_fields=["status", "completed_at", "updated_at"])

        order.refresh_from_db()
        payload = self._success_payload(order, attempt)

        # Payment succeeded even if reactivation did not — say so plainly
        # rather than telling the customer the whole thing failed.
        if attempt and attempt.status != "success":
            payload["success"] = True
            payload["reactivation_pending"] = True
            payload["message"] = (
                "Payment successful. SIM reactivation is still processing — "
                "it will be retried automatically."
            )

        return Response(payload)

    @staticmethod
    def _success_payload(order, attempt=None):
        if attempt is None:
            attempt = order.reactivation_attempts.order_by("-updated_at").first()
        return {
            "success": True,
            "message": "Payment successful! Your recharge has been processed.",
            "order_ref": order.order_ref,
            "order_number": order.order_ref,
            "status": order.status,
            "status_label": order.get_status_display(),
            "msisdn": order.msisdn,
            "amount": order.amount_display,
            "product_name": order.product.name if order.product else None,
            "reactivation_status": attempt.status if attempt else None,
            "reactivation_transaction_id": (
                attempt.provider_transaction_id if attempt else None
            ),
            "reactivation_pending": False,
        }


# ── Stripe Webhook ───────────────────────────────────────────────────────

@csrf_exempt  # nosemgrep: no-csrf-exempt -- Stripe webhook, verified via signature in construct_webhook_event(), not CSRF token
def stripe_webhook(request):
    """POST /api/recharge/webhook/

    Source of truth for 'paid'. After payment confirmation, triggers
    Transatel SIM reactivation.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    try:
        event = services.construct_webhook_event(payload, sig_header)
    except services.StripeNotConfigured as exc:
        logger.error("Stripe webhook rejected: not configured (%s)", exc)
        return HttpResponse("Service unavailable", status=503)
    except ValueError:
        return HttpResponse("Invalid payload", status=400)
    except Exception:
        return HttpResponse("Invalid signature", status=400)

    etype = event["type"]
    logger.info("Stripe webhook received: %s", etype)

    if etype == "checkout.session.completed":
        session = event["data"]["object"]
        ref = (session.metadata or {}).get("order_ref")
        order = RechargeOrder.objects.filter(order_ref=ref).first()

        if order and order.status == RechargeOrder.STATUS_PENDING:
            # Mark as processing (paid but reactivation pending)
            order.status = RechargeOrder.STATUS_PROCESSING
            order.stripe_payment_intent_id = session.payment_intent or ""
            order.paid_at = timezone.now()
            order.save(update_fields=["status", "stripe_payment_intent_id", "paid_at", "updated_at"])

            logger.info("Payment confirmed for %s — triggering reactivation", order.order_ref)

            # ── TRIGGER TRANSATEL REACTIVATION ──
            if order.sim_serial:
                try:
                    attempt = reactivation_service.reactivate_for_order(order)
                    if attempt and attempt.status == "success":
                        logger.info("Reactivation succeeded for %s", order.order_ref)
                    else:
                        logger.warning(
                            "Reactivation not successful for %s (status=%s). Will retry.",
                            order.order_ref,
                            attempt.status if attempt else "no_attempt",
                        )
                except Exception:
                    logger.exception(
                        "Reactivation error for %s — queued for retry",
                        order.order_ref,
                    )
            else:
                logger.warning(
                    "Order %s has no sim_serial — skipping reactivation",
                    order.order_ref,
                )
                # Still mark completed for non-SIM recharges (e.g. top-ups)
                order.status = RechargeOrder.STATUS_COMPLETED
                order.completed_at = timezone.now()
                order.save(update_fields=["status", "completed_at", "updated_at"])

    elif etype in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        session = event["data"]["object"]
        ref = (session.get("metadata") or {}).get("order_ref")
        order = RechargeOrder.objects.filter(order_ref=ref).first()
        if order and order.status == RechargeOrder.STATUS_PENDING:
            order.status = RechargeOrder.STATUS_FAILED
            order.save(update_fields=["status", "updated_at"])
            logger.info("Payment failed/expired for %s", order.order_ref)

    return JsonResponse({"received": True})


# ── Transatel Logs (admin endpoint) ──────────────────────────────────────

class TransatelLogsView(APIView):
    """GET /api/recharge/transatel-logs/  — recent Transatel API call logs."""
    permission_classes = [AllowAny]  # TODO: restrict to admin

    def get(self, request):
        limit = int(request.query_params.get("limit", 50))
        logs = TransatelLog.objects.all()[:limit]
        data = [
            {
                "id": log.id,
                "action": log.action,
                "sim_serial_masked": log.sim_serial_masked,
                "msisdn_masked": log.msisdn_masked,
                "request_method": log.request_method,
                "response_status": log.response_status,
                "success": log.success,
                "error_message": log.error_message,
                "duration_ms": log.duration_ms,
                "created_at": log.created_at.isoformat(),
                "order_ref": log.order.order_ref if log.order else None,
            }
            for log in logs
        ]
        return Response({"success": True, "logs": data})