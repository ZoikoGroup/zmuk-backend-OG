import json

from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework import status

from .models import RechargeModule, RechargeOrder
from .serializers import (
    RechargeModuleSerializer,
    RechargeOrderSerializer,
    CreateRechargeSerializer,
)
from . import services


class RechargeModulesView(APIView):
    """GET /api/recharge/modules/  -> the Active Modules table (Recharge / Top Up / Pending Bill)."""
    permission_classes = [AllowAny]

    def get(self, request):
        mods = RechargeModule.objects.all()
        return Response(RechargeModuleSerializer(mods, many=True).data)


class RechargeStatsView(APIView):
    """GET /api/recharge/stats/  -> dashboard totals (matches the WP dashboard cards)."""
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


class RechargeOrdersView(APIView):
    """GET /api/recharge/orders/  -> recent recharge orders (like the WP Orders table)."""
    permission_classes = [AllowAny]

    def get(self, request):
        limit = int(request.query_params.get("limit", 20))
        orders = RechargeOrder.objects.all()[:limit]
        return Response(RechargeOrderSerializer(orders, many=True).data)


class CreateRechargeView(APIView):
    """POST /api/recharge/create/  -> create order + Stripe Checkout Session.
    Returns {order_ref, checkout_url}. Frontend redirects the user to checkout_url.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = CreateRechargeSerializer(data=request.data, context={})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        amount_pence = serializer.context["amount_pence"]

        order = RechargeOrder.objects.create(
            module=data["module"],
            msisdn=data["msisdn"],
            customer_name=data.get("customer_name", "") or "",
            customer_email=data.get("customer_email", "") or "",
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
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception as exc:  # Stripe API error
            order.status = RechargeOrder.STATUS_FAILED
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


@csrf_exempt
def stripe_webhook(request):
    """POST /api/recharge/webhook/  -> Stripe calls this. Source of truth for 'paid'.
    Must be CSRF-exempt (server-to-server) but signature-verified.
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
        # Signature verification failed
        return HttpResponse("Invalid signature", status=400)

    etype = event["type"]

    if etype == "checkout.session.completed":
        session = event["data"]["object"]
        ref = (session.get("metadata") or {}).get("order_ref")
        order = RechargeOrder.objects.filter(order_ref=ref).first()
        if order and order.status != RechargeOrder.STATUS_COMPLETED:
            order.status = RechargeOrder.STATUS_COMPLETED
            order.stripe_payment_intent_id = session.get("payment_intent", "") or ""
            order.save(update_fields=["status", "stripe_payment_intent_id"])
            # --- Transatel hook (OPTIONAL) -------------------------------------
            # If Lennox confirms a post-payment connectivity refresh is required,
            # call it HERE (only after 'completed'). Transatel has no top-up API,
            # so this would be a LINE_CONNECTIVITY_REFRESH call, not a money op.
            # e.g. refresh_line_connectivity(order.msisdn)
            # -------------------------------------------------------------------

    elif etype in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        session = event["data"]["object"]
        ref = (session.get("metadata") or {}).get("order_ref")
        order = RechargeOrder.objects.filter(order_ref=ref).first()
        if order and order.status == RechargeOrder.STATUS_PENDING:
            order.status = RechargeOrder.STATUS_FAILED
            order.save(update_fields=["status"])

    return JsonResponse({"received": True})
