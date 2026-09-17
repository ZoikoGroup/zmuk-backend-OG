import logging

from django.conf import settings
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.throttling import AnonRateThrottle


class TransatelLookupThrottle(AnonRateThrottle):
    """Tight rate limit for endpoints that trigger live Transatel API calls.
    Each validate-phone/ call makes 2 Transatel requests — protect the quota."""
    rate = '30/minute'


class PaymentThrottle(AnonRateThrottle):
    """Rate limit payment creation to prevent abuse."""
    rate = '10/minute'

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

    The REAL flow — calls Transatel directly, no local DB dependency:

      1. Normalize phone number to international digits (447421118918)
      2. Call Transatel GET /subscribers/msisdn/{msisdn} → get simSerial + live status
      3. Optionally call GET /subscribers/sim-serial/{simSerial} for full detail
      4. Based on live status → determine rechargeable
      5. Log everything in TransatelLog
      6. Sync local sims_sim table with live data (cache, not source of truth)

    Request:  { "phone_number": "+447421118918" }
    """
    permission_classes = [AllowAny]
    throttle_classes = [TransatelLookupThrottle]

    def post(self, request):
        serializer = PhoneValidateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        phone = serializer.validated_data["phone_number"]

        # ── Step 1: Normalize to international digits ────────────────────
        digits = phone.lstrip("+").replace(" ", "").replace("-", "")
        if digits.startswith("0"):
            digits = "44" + digits[1:]
        # If they typed just the local part without country code
        if len(digits) <= 10 and not digits.startswith("44"):
            digits = "44" + digits

        # ── Step 2: Call Transatel /subscribers/msisdn/{msisdn} ──────────
        import time
        client = TransatelClient()
        msisdn_endpoint = (
            f"/connectivity-management/subscribers/api/subscribers"
            f"/msisdn/{digits}"
        )

        start = time.time()
        transatel_error = None
        msisdn_data = None
        sim_serial = None
        live_status = None
        full_subscriber = None

        log_entry = TransatelLog(
            action="validate_phone_live",
            msisdn_masked=mask_identifier(phone),
            request_url=f"{{base}}{msisdn_endpoint}",
            request_method="GET",
        )

        try:
            result = client.get(msisdn_endpoint)
            duration = int((time.time() - start) * 1000)
            log_entry.response_status = result.get("status_code", 0)
            log_entry.duration_ms = duration

            if result.get("success"):
                msisdn_data = result.get("data", {})
                sim_serial = msisdn_data.get("simSerial", "")
                live_status = (
                    msisdn_data.get("status")
                    or msisdn_data.get("simStatus")
                    or ""
                ).strip()
                log_entry.sim_serial_masked = mask_identifier(sim_serial)
                log_entry.response_body = msisdn_data
                log_entry.success = True
            else:
                error_data = result.get("data", {})
                transatel_error = (
                    error_data.get("detail")
                    or error_data.get("title")
                    or f"HTTP {result.get('status_code', 'unknown')}"
                )
                log_entry.response_body = error_data
                log_entry.error_message = transatel_error
                log_entry.success = False

        except TransatelAPIError as exc:
            duration = int((time.time() - start) * 1000)
            transatel_error = str(exc)
            log_entry.response_status = getattr(exc, "status_code", 0)
            log_entry.error_message = transatel_error
            log_entry.success = False
            log_entry.duration_ms = duration
            logger.warning("Transatel MSISDN lookup failed for %s: %s", digits, exc)

        except Exception as exc:
            duration = int((time.time() - start) * 1000)
            transatel_error = str(exc)
            log_entry.error_message = transatel_error
            log_entry.success = False
            log_entry.duration_ms = duration
            logger.exception("Unexpected error during Transatel MSISDN lookup")

        log_entry.save()

        # If Transatel couldn't find the number
        if not sim_serial:
            return Response(
                {
                    "success": False,
                    "message": "Phone number not found on the network.",
                    "transatel_error": transatel_error,
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        # ── Step 3: Get full subscriber detail ───────────────────────────
        serial_endpoint = (
            f"/connectivity-management/subscribers/api/subscribers"
            f"/sim-serial/{sim_serial}"
        )

        start2 = time.time()
        log_entry2 = TransatelLog(
            action="get_subscriber_detail",
            sim_serial_masked=mask_identifier(sim_serial),
            msisdn_masked=mask_identifier(phone),
            request_url=f"{{base}}{serial_endpoint}",
            request_method="GET",
        )

        try:
            result2 = client.get(serial_endpoint)
            duration2 = int((time.time() - start2) * 1000)
            log_entry2.response_status = result2.get("status_code", 0)
            log_entry2.duration_ms = duration2

            if result2.get("success"):
                full_subscriber = result2.get("data", {})
                # Use the more detailed status if available
                detailed_status = (
                    full_subscriber.get("status")
                    or full_subscriber.get("simStatus")
                    or ""
                ).strip()
                if detailed_status:
                    live_status = detailed_status
                log_entry2.response_body = full_subscriber
                log_entry2.success = True
            else:
                log_entry2.success = False
                log_entry2.error_message = str(result2.get("data", {}))

        except Exception as exc:
            duration2 = int((time.time() - start2) * 1000)
            log_entry2.error_message = str(exc)
            log_entry2.success = False
            log_entry2.duration_ms = duration2

        log_entry2.save()

        # ── Step 4: Determine rechargeable ───────────────────────────────
        sim_status = live_status or "Unknown"
        is_suspended = sim_status.lower() == "suspended"
        is_active = sim_status.lower() == "active"

        # Per the corrected handover doc and confirmed live testing:
        # - Active SIMs: reactivation not needed, but top-up/plan change may apply
        # - Suspended SIMs: reactivation needed after payment
        # - Terminated SIMs: cannot be recharged
        rechargeable = sim_status.lower() in ("active", "suspended")
        needs_reactivation = is_suspended

        if sim_status.lower() == "terminated":
            message = "This SIM has been terminated and cannot be recharged."
            rechargeable = False
        elif is_active:
            message = "Phone number validated. SIM is active — plan top-up available."
        elif is_suspended:
            message = "Phone number validated. SIM is suspended — recharge will reactivate."
        else:
            message = f"Phone number found. Current status: {sim_status}."

        # ── Step 5: Sync local DB (cache, not source of truth) ───────────
        try:
            variants = phone_variants(phone, country_code="44")
            sim_obj = Sim.objects.filter(msisdn__in=variants).first()
            if sim_obj:
                sim_obj.provisioning_status = live_status
                if sim_serial:
                    sim_obj.serial_number = sim_serial
                sim_obj.save(update_fields=["provisioning_status", "serial_number"])
            else:
                # Create a local cache entry from live data
                Sim.objects.create(
                    msisdn=phone,
                    serial_number=sim_serial,
                    iccid=sim_serial,
                    provisioning_status=live_status or "",
                    imsi=full_subscriber.get("primaryImsi", "") if full_subscriber else "",
                )
        except Exception as exc:
            logger.warning("Could not sync local SIM cache: %s", exc)

        # ── Step 6: Build response ───────────────────────────────────────
        sim_data = {
            "phone_number": phone,
            "sim_card_id_masked": mask_identifier(sim_serial),
            "sim_iccid_masked": mask_identifier(sim_serial),
            "sim_status": sim_status,
            "rechargeable": rechargeable,
            "is_suspended": is_suspended,
            "is_active": is_active,
            "needs_reactivation": needs_reactivation,
        }

        response_payload = {
            "success": True,
            "message": message,
            "sim": SimDetailSerializer(sim_data).data,
            "sim_serial": sim_serial,
            "sim_iccid": sim_serial,
            "status_source": "transatel_live",
            "transatel_live": {
                "checked": True,
                "status": live_status,
                "error": None,
                "rate_plan": msisdn_data.get("ratePlan") if msisdn_data else None,
                "service_profile": msisdn_data.get("serviceProfile") if msisdn_data else None,
                "activation_date": msisdn_data.get("activationDate") if msisdn_data else None,
                "group": msisdn_data.get("group") if msisdn_data else None,
                "full_subscriber": full_subscriber,
            },
        }

        return Response(response_payload)


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
    permission_classes = [IsAdminUser]

    def get(self, request):
        mods = RechargeModule.objects.all()
        return Response(RechargeModuleSerializer(mods, many=True).data)


# ── Stats ────────────────────────────────────────────────────────────────

class RechargeStatsView(APIView):
    """GET /api/recharge/stats/"""
    permission_classes = [IsAdminUser]

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
    permission_classes = [IsAdminUser]

    def get(self, request):
        limit = int(request.query_params.get("limit", 20))
        orders = RechargeOrder.objects.all()[:limit]
        return Response(RechargeOrderSerializer(orders, many=True).data)


# ── Order Detail ─────────────────────────────────────────────────────────

class RechargeOrderDetailView(APIView):
    """GET /api/recharge/orders/<order_ref>/"""
    permission_classes = [IsAdminUser]

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
    throttle_classes = [PaymentThrottle]

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
    throttle_classes = [PaymentThrottle]

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
    throttle_classes = [PaymentThrottle]

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
    permission_classes = [IsAdminUser]

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