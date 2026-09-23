from django.utils import timezone
from apps.coupons.models import Coupon, CouponUsage


def validate_coupon(coupon, user, amount, plan=None):
    # 1️⃣ Base coupon validation (status, date, limit)
    valid, message = coupon.is_valid()
    if not valid:
        return False, message, 0

    # 2️⃣ User validation (ONLY if users are assigned)
    if coupon.users.exists():
        if not user:
            return False, "Login required for this coupon", 0

        if not coupon.users.filter(id=user.id).exists():
            return False, "Coupon not valid for this user", 0

    # 3️⃣ One-time per customer
    if coupon.is_use_once_per_customer and user:
        if CouponUsage.objects.filter(coupon=coupon, user=user).exists():
            return False, "Coupon already used by this user", 0

    # 4️⃣ Plan validation (ONLY if plans are assigned)
    if coupon.plans.exists():
        if not plan:
            return False, "Coupon requires a valid plan", 0

        if not coupon.plans.filter(id=plan.id).exists():
            return False, "Coupon not valid for this plan", 0

    # 5️⃣ Calculate discount
    if coupon.type == Coupon.FLAT:
        discount = float(coupon.discount)
    else:
        discount = (float(coupon.discount) / 100) * float(amount)

    discount = min(discount, float(amount))

    return True, "Coupon valid", round(discount, 2)
