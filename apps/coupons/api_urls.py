from django.urls import path
from . import api_views

app_name = "coupons_api"

urlpatterns = [
    path(
        "preview-coupon/",
        api_views.CouponPreviewAPI.as_view(),
        name="preview_coupon"
    ),
    path(
        "apply-coupon/",
        api_views.CouponApplyAPI.as_view(),
        name="apply_coupon"
    ),
]
