from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework import status

from .models import SimProduct, SimOrder, SimActivationCode
from .serializers import (
    SimProductSerializer,
    SimOrderSerializer,
    CreateSimOrderSerializer,
)
from . import services
from .emails import send_activation_code_email


class SimProductsView(APIView):
    """GET /api/sim/products/  -> SIMs available to buy."""
    permission_classes = [AllowAny]

    def get(self, request):
        products = SimProduct.objects.filter(is_active=True)
        return Response(SimProductSerializer(products, many=True).data)


class BuySimView(APIView):
    """POST /api/sim/buy/  -> create order + Stripe Checkout Session.
    Returns {order_ref, checkout_url}. Frontend redirects to checkout_url.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = CreateSimOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        product = data["product"]

        order = SimOrder.objects.create(
            product=product,
            customer_name=data.get("customer_name", "") or "",
            email=data["email"],
            amount_pence=product.price_pence,
            currency=product.currency,
            status=SimOrder.STATUS_PENDING,
        )

        try:
            session = services.create_checkout_session(
                order, success_url=data["success_url"], cancel_url=data["cancel_url"]
            )
        except services.StripeNotConfigured as exc:
            order.status = SimOrder.STATUS_FAILED
            order.save(update_fields=["status"])
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception as exc:
            order.status = SimOrder.STATUS_FAILED
            order.save(update_fields=["status"])
            return Response(
                {"detail": f"Payment could not be started: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        order.stripe_session_id = session.get("id", "")
        order.save(update_fields=["stripe_session_id"])
        return Response(
            {"order_ref": order.order_ref, "checkout_url": session.get("url")},
            status=status.HTTP_201_CREATED,
        )


class SimOrdersView(APIView):
    """GET /api/sim/orders/  -> recent SIM orders."""
    permission_classes = [AllowAny]

    def get(self, request):
        limit = int(request.query_params.get("limit", 20))
        orders = SimOrder.objects.all()[:limit]
        return Response(SimOrderSerializer(orders, many=True).data)


@csrf_exempt
def stripe_webhook(request):
    """POST /api/sim/webhook/  -> Stripe calls this. On payment success we issue
    the ZM###### activation code and email it (the Django version of the WP
    'woocommerce_thankyou' hook). Signature-verified, CSRF-exempt, idempotent.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    try:
        event = services.construct_webhook_event(payload, sig_header)
    except services.StripeNotConfigured as exc:
        return HttpResponse(str(exc), status=503)
    except ValueError:
        return HttpResponse("Invalid payload", status=400)
    except Exception:
        return HttpResponse("Invalid signature", status=400)

    etype = event["type"]

    if etype == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata") or {}
        if meta.get("kind") != "sim_purchase":
            return JsonResponse({"received": True, "ignored": "not a sim purchase"})

        order = SimOrder.objects.filter(order_ref=meta.get("order_ref")).first()
        if order and order.status != SimOrder.STATUS_COMPLETED:
            order.status = SimOrder.STATUS_COMPLETED
            order.stripe_payment_intent_id = session.get("payment_intent", "") or ""
            order.save(update_fields=["status", "stripe_payment_intent_id"])

            # Idempotent: only issue a code if this order doesn't already have one.
            if not order.activation_codes.exists():
                code_obj = SimActivationCode.issue_for_order(order)
                send_activation_code_email(order, code_obj.code)

    elif etype in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        session = event["data"]["object"]
        meta = session.get("metadata") or {}
        order = SimOrder.objects.filter(order_ref=meta.get("order_ref")).first()
        if order and order.status == SimOrder.STATUS_PENDING:
            order.status = SimOrder.STATUS_FAILED
            order.save(update_fields=["status"])

    return JsonResponse({"received": True})


from .models import SimCartOrder  # noqa: E402


class SimCheckoutOrderView(APIView):
    """POST /api/sim/checkout-order/  -> store a SIM order from the checkout page.

    Body is the checkout payload (SIM items only):
      { billingAddress:{...}, cart:[...], totals:{...}, order_type:"sim" }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data or {}
        billing = data.get("billingAddress") or {}
        email = (billing.get("email") or "").strip()
        cart = data.get("cart") or []

        if not email:
            return Response({"detail": "Billing email is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not cart:
            return Response({"detail": "No SIM items in the order."}, status=status.HTTP_400_BAD_REQUEST)

        name = f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip()
        order = SimCartOrder.objects.create(
            email=email,
            customer_name=name,
            order_type=data.get("order_type", "sim"),
            raw_data=data,
        )
        return Response(
            {"success": True, "order_ref": order.order_ref, "id": order.id},
            status=status.HTTP_201_CREATED,
        )


class SimCheckoutOrdersByUserView(APIView):
    """POST /api/sim/checkout-orders/by-user/  { logged_user: email } -> that user's SIM orders."""
    permission_classes = [AllowAny]

    def post(self, request):
        email = (request.data.get("logged_user") or "").strip()
        if not email:
            return Response({"status": False, "message": "logged_user is required"}, status=status.HTTP_400_BAD_REQUEST)

        orders = SimCartOrder.objects.filter(email=email)
        out = []
        for o in orders:
            out.append({
                "order_ref": o.order_ref,
                "order_db_id": o.id,
                "status": o.status,
                "order_type": o.order_type,
                "created_at": o.created_at,
                "data": o.raw_data,
            })
        return Response({"status": True, "orders": out}, status=status.HTTP_200_OK)
