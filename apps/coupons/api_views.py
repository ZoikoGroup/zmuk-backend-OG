from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from django.shortcuts import get_object_or_404
from django.db.models import Q

from apps.coupons.models import Coupon, CouponUsage
from apps.plans.models import Plan

from .serializers import CouponPreviewSerializer, CouponApplySerializer
from .services import validate_coupon



def get_coupon_by_code(code):
    """
    Fetch coupon by slug OR name (case-insensitive)
    """
    return get_object_or_404(
        Coupon,
        Q(slug__iexact=code) | Q(name__iexact=code)
    )


class CouponPreviewAPI(APIView):
    """
    Preview coupon (NO calculation, NO DB write)
    """

    def post(self, request):
        serializer = CouponPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        code = serializer.validated_data["code"]

        coupon = get_coupon_by_code(code)

        # ✅ Basic validation only
        valid, message = coupon.is_valid()

        if not valid:
            return Response(
                {"valid": False, "error": message},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response({
            "valid": True,
            "coupon": {
                "name": coupon.name,
                "code": coupon.slug,
                "type": coupon.type,
                "discount": coupon.discount,
                "is_use_once_per_customer": coupon.is_use_once_per_customer,
                "has_plan_restriction": coupon.plans.exists(),
                "allowed_plan_ids": list(coupon.plans.values_list("id", flat=True)),

                "has_user_restriction": coupon.users.exists(),
                "allowed_user_ids": list(coupon.users.values_list("id", flat=True)),

                "valid_till": coupon.valid_till,
                "limit": coupon.limit,
                "status": coupon.status
            }
        })


class CouponApplyAPI(APIView):
    """
    Apply coupon - preview only, no DB write at this stage
    """
    def post(self, request):
        serializer = CouponApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        coupon_code = serializer.validated_data["coupon_code"]  # ← fixed
        plan_id = serializer.validated_data.get("plan_id")

        coupon = get_coupon_by_code(coupon_code)

        valid, message = coupon.is_valid()
        if not valid:
            return Response(
                {"success": False, "message": message},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response({
            "success": True,
            "data": {
                "type": coupon.type,
                "discount": str(coupon.discount),
            },
            "message": f"Coupon applied! {'{}%'.format(coupon.discount) if coupon.type == 'percentage' else '${}'.format(coupon.discount)} off"
        })
