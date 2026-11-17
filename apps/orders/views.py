import logging
from decimal import InvalidOperation

from django.conf import settings
from django.db.models import F
from django.core.mail import EmailMultiAlternatives
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.views.decorators.csrf import csrf_exempt

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework import status
from .serializers import BqOrderSerializer
from .models import BqOrder, CheckoutPayment
from .pricing import CheckoutError, money, price_cart
from apps.coupons.models import Coupon, CouponUsage
from collections import defaultdict

import stripe

from core.notify import notify_team_of_purchase
from core.stripe_compat import to_plain

logger = logging.getLogger("apps.orders")


def _send_checkout_receipt(payment):
    """Send order confirmation / receipt email to the customer."""
    if not payment.email:
        return
    try:
        data = payment.payload or {}
        billing = data.get("billingAddress", {})
        cart = data.get("cart", [])
        customer_name = f"{billing.get('firstName', '')} {billing.get('lastName', '')}".strip() or "Customer"

        cart_items = []
        for item in cart:
            cart_items.append({
                "name": item.get("title") or item.get("name") or "Item",
                "qty": item.get("quantity", 1),
                "price": f"£{float(item.get('price', 0)):.2f}",
                "line_total": f"£{float(item.get('price', 0)) * int(item.get('quantity', 1)):.2f}",
            })

        ctx = {
            "customer_name": customer_name,
            "order_ref": payment.order_ref,
            "cart_items": cart_items,
            "total": f"£{payment.amount_pence / 100:.2f}",
            "email": payment.email,
            "created_at": payment.created_at.strftime("%d %b %Y, %H:%M"),
        }

        html_body = render_to_string("emails/checkout_receipt.html", ctx)
        text_body = strip_tags(html_body)
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@zoikomobile.co.uk"

        msg = EmailMultiAlternatives(
            subject=f"Zoiko Mobile — Order Confirmation {payment.order_ref}",
            body=text_body,
            from_email=from_email,
            to=[payment.email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send()
        logger.info("Receipt email sent to %s for order %s", payment.email, payment.order_ref)
    except Exception:
        logger.exception("Failed to send receipt email for %s", payment.order_ref)


def _notify_team_of_checkout(payment):
    """Tell the Zoiko team a website checkout was paid (best effort, never raises)."""
    try:
        data = payment.payload or {}
        items = []
        for item in data.get("cart", []) or []:
            try:
                qty = int(item.get("quantity", 1))
                price = float(item.get("price", 0))
            except (TypeError, ValueError):
                qty, price = 1, 0.0
            name = item.get("title") or item.get("name") or "Item"
            items.append(f"{name} x {qty} - £{price * qty:.2f}")
        notify_team_of_purchase(
            "Website checkout",
            payment.order_ref,
            payment.email,
            f"£{payment.amount_pence / 100:.2f}",
            items=items,
            billing=data.get("billingAddress"),
            shipping=data.get("shippingAddress"),
        )
    except Exception:
        logger.exception("Could not build purchase alert for %s", payment.order_ref)


class BqOrderCreateAPIView(APIView):
    def post(self, request):
        serializer = BqOrderSerializer(
            data={"raw_data": request.data}
        )

        if serializer.is_valid():
            serializer.save()
            return Response(
                {"success": True, "message": "Order saved successfully"},
                status=status.HTTP_201_CREATED
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class BqUserGroupedOrdersAPIView(APIView):
    """
    POST BODY:
    {
        "logged_user": "user@example.com"
    }
    """

    def post(self, request):
        logged_user = request.data.get("logged_user")

        if not logged_user:
            return Response({
                "status": False,
                "message": "logged_user is required"
            }, status=status.HTTP_400_BAD_REQUEST)

        orders = BqOrder.objects.all().order_by("-id")

        grouped_data = defaultdict(lambda: defaultdict(list))

        for order in orders:
            data = order.raw_data  # already dict

            billing = data.get("billingAddress", {})
            order_email = billing.get("email", "")

            if order_email != logged_user:
                continue

            cart = data.get("cart", [])
            totals = data.get("totals", {})

            grouped_data[order_email][str(order.id)].append({
                "order_db_id": order.id,
                "bequick_order_id": data.get("bequick_order_id"),
                "subscriber_id": data.get("subscriber_id"),
                "total": totals.get("total"),
                "subtotal": totals.get("subtotal"),
                "shipping": totals.get("shipping"),
                "discount": totals.get("discount"),
                "payment_method": data.get("paymentMethod"),
                "cart": cart,
                "created_at": order.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            })

        return Response({
            "status": True,
            "logged_user": logged_user,
            "groups": grouped_data
        })


# ── Checkout: create PaymentIntent (server-side, source of truth) ────────

class CheckoutCreateIntentView(APIView):
    """POST /api/v1/checkout/create-intent/

    Creates a CheckoutPayment row (holds full cart/billing payload) and a
    Stripe PaymentIntent. Only order_ref goes into Stripe metadata — the
    full payload lives in our DB, looked up by the webhook after payment.

    Request body: { total, cart, couponCode?, billingAddress, shippingAddress }
    (`subtotal` / `discountAmount` may still be sent but are ignored.)
    Send `Authorization: Token …` when logged in; it is required for public
    sector / student plans and for coupons.
    Response: { clientSecret, order_ref, totals }

    Prices, the coupon discount and the total are worked out on the server
    (apps/orders/pricing.py). `total` is what the customer saw: if it differs
    from ours we refuse with 409 PRICE_CHANGED and nothing is charged.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data or {}

        try:
            client_total = money(data.get("total"))
        except (InvalidOperation, TypeError, ValueError):
            client_total = None
        if client_total is None or client_total <= 0:
            return Response(
                {"error": "Invalid total amount", "code": "TOTAL_INVALID", "message": "Invalid total amount"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        coupon_code = data.get("couponCode") or data.get("coupon_code") or ""
        try:
            priced = price_cart(data.get("cart"), request.user, coupon_code)
        except CheckoutError as exc:
            return Response(exc.as_dict(), status=exc.http_status)

        if priced.total <= 0:
            message = "There's nothing to pay for this order."
            return Response(
                {"error": message, "code": "TOTAL_INVALID", "message": message},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if priced.total != client_total:
            message = "Prices in your cart have changed. Please review your order."
            return Response(
                {
                    "error": message,
                    "code": "PRICE_CHANGED",
                    "message": message,
                    "totals": priced.totals(),
                    "lines": priced.summary_lines(),
                },
                status=status.HTTP_409_CONFLICT,
            )

        billing = data.get("billingAddress") or {}
        email = (billing.get("email") or "").strip()

        # Store our verified lines and totals, not the browser's prices: the
        # receipt, team alert and order rows are all built from this payload.
        payload = {
            **data,
            "cart": priced.lines,
            "totals": priced.totals(),
            "subtotal": float(priced.subtotal),
            "discountAmount": float(priced.discount),
            "total": float(priced.total),
            "coupon": (
                {"id": priced.coupon.pk, "code": priced.coupon.slug, "discount": str(priced.discount)}
                if priced.coupon
                else None
            ),
        }
        payload.pop("couponCode", None)
        payload.pop("coupon_code", None)

        payment = CheckoutPayment.objects.create(
            email=email,
            user=request.user if request.user.is_authenticated else None,
            amount_pence=int(priced.total * 100),
            currency="gbp",
            payload=payload,
        )

        key = getattr(settings, "STRIPE_SECRET_KEY", "") or ""
        if not key:
            payment.status = CheckoutPayment.STATUS_FAILED
            payment.save(update_fields=["status"])
            return Response({"error": "Payments are not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        stripe.api_key = key

        try:
            intent = stripe.PaymentIntent.create(
                amount=payment.amount_pence,
                currency=payment.currency,
                automatic_payment_methods={"enabled": True},
                receipt_email=email or None,
                metadata={"order_ref": payment.order_ref, "source": "zoiko_checkout"},
                idempotency_key=f"checkout-intent-{payment.order_ref}",
            )
        except Exception as exc:
            payment.status = CheckoutPayment.STATUS_FAILED
            payment.save(update_fields=["status"])
            logger.exception("Checkout PaymentIntent creation failed for %s", payment.order_ref)
            return Response({"error": f"Payment initialization failed: {exc}"}, status=status.HTTP_502_BAD_GATEWAY)

        payment.stripe_payment_intent_id = intent.id
        payment.save(update_fields=["stripe_payment_intent_id"])

        return Response({
            "clientSecret": intent.client_secret,
            "order_ref": payment.order_ref,
            "totals": priced.totals(),
        })


# ── Checkout: poll order status after payment (used by success screen) ───

class CheckoutOrderStatusView(APIView):
    """GET /api/v1/checkout/order-status/<order_ref>/

    Public — the frontend polls this after stripe.confirmPayment() succeeds,
    waiting for the WEBHOOK (not the browser) to have processed the order.
    This is what replaces the old client-side SIM_ORDER_URL / ORDER_URL POSTs.
    """
    permission_classes = [AllowAny]

    def get(self, request, order_ref):
        payment = CheckoutPayment.objects.filter(order_ref=order_ref).first()
        if not payment:
            return Response({"found": False}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "found": True,
            "order_ref": payment.order_ref,
            "status": payment.status,        # pending | paid | failed
            "processed": payment.processed,  # true once order rows exist
            "stripe_payment_intent_id": payment.stripe_payment_intent_id or "",
        })


class ConfirmCheckoutView(APIView):
    """POST /api/v1/checkout/confirm/

    Called by the browser after Stripe payment succeeds.
    Retrieves the PaymentIntent from Stripe directly to verify payment,
    then marks the order as paid and processes it.
    No webhook needed — works without Stripe CLI.

    Request: { order_ref, payment_intent_id }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        order_ref = (request.data.get("order_ref") or "").strip()
        pi_id = (request.data.get("payment_intent_id") or "").strip()

        if not order_ref or not pi_id:
            return Response({"success": False, "message": "order_ref and payment_intent_id are required."}, status=400)

        payment = CheckoutPayment.objects.filter(order_ref=order_ref).first()
        if not payment:
            return Response({"success": False, "message": "Order not found."}, status=404)

        if payment.processed:
            return Response({"success": True, "order_ref": payment.order_ref, "processed": True})

        key = getattr(settings, "STRIPE_SECRET_KEY", "") or ""
        if not key:
            return Response({"success": False, "message": "Payment service not configured."}, status=503)

        stripe.api_key = key

        try:
            intent = stripe.PaymentIntent.retrieve(pi_id)
        except Exception as exc:
            logger.exception("Could not retrieve PaymentIntent %s for %s", pi_id, order_ref)
            return Response({"success": False, "message": f"Could not verify payment: {exc}"}, status=502)

        if intent.status != "succeeded":
            return Response({
                "success": False,
                "message": f"Payment not completed (status: {intent.status}).",
            }, status=402)

        if payment.status != CheckoutPayment.STATUS_PAID:
            payment.status = CheckoutPayment.STATUS_PAID
            payment.stripe_payment_intent_id = pi_id
            payment.save(update_fields=["status", "stripe_payment_intent_id"])

        try:
            _process_checkout_payment(payment)
        except Exception:
            logger.exception("Failed to process checkout order %s", order_ref)

        return Response({
            "success": True,
            "order_ref": payment.order_ref,
            "processed": payment.processed,
        })

def _record_coupon_usage(payment: CheckoutPayment):
    coupon = (payment.payload or {}).get("coupon") or {}
    if not coupon.get("id"):
        return
    try:
        CouponUsage.objects.create(
            coupon_id=coupon["id"], user=payment.user, order_id=payment.order_ref
        )
        Coupon.objects.filter(pk=coupon["id"]).update(used_count=F("used_count") + 1)
    except Exception:
        logger.exception("Could not record coupon usage for %s", payment.order_ref)


def _process_checkout_payment(payment: CheckoutPayment):
    """Split the paid cart into SIM orders + BqOrders, then send receipt email.
    Idempotent — safe to call twice (webhook + confirm-poll can both land)."""
    if payment.processed:
        return

    from apps.sim_orders.models import SimCartOrder

    data = payment.payload or {}
    cart = data.get("cart", [])

    def is_sim(item):
        st = str(item.get("simType") or item.get("metadata", {}).get("simType") or "").lower()
        return "esim" in st or "psim" in st

    sim_lines = [i for i in cart if is_sim(i)]
    other_lines = [i for i in cart if not is_sim(i)]

    if sim_lines:
        SimCartOrder.objects.create(
            email=payment.email,
            customer_name=f"{data.get('billingAddress', {}).get('firstName', '')} "
                           f"{data.get('billingAddress', {}).get('lastName', '')}".strip(),
            order_type="sim",
            raw_data={**data, "cart": sim_lines},
        )

    if other_lines:
        BqOrder.objects.create(raw_data={**data, "cart": other_lines})

    payment.processed = True
    payment.save(update_fields=["processed"])

    # Count the coupon as used only once the order is paid (limit / once per customer).
    _record_coupon_usage(payment)

    # Send receipt email to customer
    _send_checkout_receipt(payment)

    # Tell the team (runs once: the 'processed' guard above stops repeats)
    _notify_team_of_checkout(payment)


@csrf_exempt  # nosemgrep: no-csrf-exempt -- Stripe webhook, verified via signature, not CSRF token
def checkout_webhook(request):
    """POST /api/v1/checkout/webhook/

    Source of truth for 'paid' on the main checkout flow. On
    payment_intent.succeeded, marks the CheckoutPayment paid and creates the
    real order rows — independent of whether the browser's own order-save
    call succeeded.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    secret = (
        getattr(settings, "STRIPE_CHECKOUT_WEBHOOK_SECRET", "")
        or getattr(settings, "STRIPE_WEBHOOK_SECRET", "")
        or ""
    )
    if not secret:
        logger.error("Checkout webhook rejected: no signing secret set (STRIPE_CHECKOUT_WEBHOOK_SECRET / STRIPE_WEBHOOK_SECRET)")
        return HttpResponse("Service unavailable", status=503)

    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except ValueError:
        return HttpResponse("Invalid payload", status=400)
    except stripe.error.SignatureVerificationError:
        return HttpResponse("Invalid signature", status=400)

    event = to_plain(event)  # stripe>=15 objects are not dicts: .get() would raise
    etype = event["type"]

    if etype == "payment_intent.succeeded":
        intent = event["data"]["object"]
        ref = (intent.get("metadata") or {}).get("order_ref")
        if not ref:
            return JsonResponse({"received": True, "ignored": "no order_ref"})

        payment = CheckoutPayment.objects.filter(order_ref=ref).first()
        if payment:
            if payment.status != CheckoutPayment.STATUS_PAID:
                payment.status = CheckoutPayment.STATUS_PAID
                payment.save(update_fields=["status"])
            # _process_checkout_payment is idempotent (no-op once processed), so retrying
            # here also rescues an order the browser-confirm call marked paid but then
            # failed to process.
            if not payment.processed:
                try:
                    _process_checkout_payment(payment)
                except Exception:
                    logger.exception("Failed to process checkout order for %s — will need manual review", ref)

    elif etype == "payment_intent.payment_failed":
        intent = event["data"]["object"]
        ref = (intent.get("metadata") or {}).get("order_ref")
        payment = CheckoutPayment.objects.filter(order_ref=ref).first()
        if payment and payment.status == CheckoutPayment.STATUS_PENDING:
            payment.status = CheckoutPayment.STATUS_FAILED
            payment.save(update_fields=["status"])

    return JsonResponse({"received": True})
