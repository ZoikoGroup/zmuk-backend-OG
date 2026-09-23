from django.conf import settings
from django.db import models
from django.utils import timezone
from apps.plans.models import Plan


class Coupon(models.Model):
    FLAT = "flat"
    PERCENTAGE = "percentage"

    TYPE_CHOICES = [
        (FLAT, "Flat"),
        (PERCENTAGE, "Percentage"),
    ]

    # ✅ STATUS
    ACTIVE = "active"
    INACTIVE = "inactive"
    EXPIRED = "expired"

    STATUS_CHOICES = [
        (ACTIVE, "Active"),
        (INACTIVE, "Inactive"),
        (EXPIRED, "Expired"),
    ]

    id = models.BigAutoField(primary_key=True)

    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)

    limit = models.IntegerField(null=True, blank=True)
    used_count = models.PositiveIntegerField(default=0)

    # ✅ MULTI USERS
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="assigned_coupons",
        help_text="Leave empty = valid for all users"
    )

    discount = models.DecimalField(max_digits=10, decimal_places=2, default="0.00")

    type = models.CharField(
        max_length=20,
        choices=TYPE_CHOICES,
        default=FLAT
    )

    is_use_once_per_customer = models.BooleanField(default=True)

    # ✅ MULTI PLANS
    plans = models.ManyToManyField(
        Plan,
        blank=True,
        related_name="coupons",
        help_text="Leave empty = valid for all plans"
    )

    # 🔥 NEW FIELDS
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=ACTIVE
    )

    valid_from = models.DateTimeField(null=True, blank=True)
    valid_till = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "coupons"

    def __str__(self):
        return self.name

    # ✅ CENTRAL VALIDATION METHOD
    def is_valid(self):
        now = timezone.now()

        if self.status != self.ACTIVE:
            return False, "Coupon is inactive"

        if self.valid_from and now < self.valid_from:
            return False, "Coupon not active yet"

        if self.valid_till and now > self.valid_till:
            return False, "Coupon expired"

        if self.limit is not None and self.used_count >= self.limit:
            return False, "Coupon usage limit reached"

        return True, "Coupon is valid"


class CouponUsage(models.Model):
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    order_id = models.CharField(max_length=100)
    applied_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'coupon_usages'
