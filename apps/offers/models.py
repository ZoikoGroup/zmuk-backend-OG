from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.coupons.models import Coupon

MAX_BULLETS = 4  # the offers page shows at most four bullet points


def validate_internal_link(value):
    """Only links inside our own site (e.g. /plans), never another site or a script."""
    if value and (not value.startswith("/") or value.startswith("//")):
        raise ValidationError('Use a link inside the site that starts with "/", for example /plans.')


class Offer(models.Model):
    """A limited-time offer card on the dashboard "Latest offers" page.

    The card only displays the offer; it never changes a price. A coupon
    offer shows the coupon's code and discount, and checkout checks the code
    again as usual. An information offer has no code and no discount.

    The public sector and student discounts are not offers here: they are
    always on and come from their own apps.
    """

    class Mechanism(models.TextChoices):
        COUPON = "coupon", "Coupon code"
        INFO = "info", "Information only (no discount)"

    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=120)
    subtitle = models.CharField(max_length=255, blank=True)
    badge = models.CharField(max_length=40, default="Limited time")
    bullets = models.JSONField(
        default=list,
        blank=True,
        help_text=f"Up to {MAX_BULLETS} short points shown on the card.",
    )
    terms_text = models.TextField(blank=True, help_text="The small print shown on the card.")

    mechanism = models.CharField(
        max_length=20,
        choices=Mechanism.choices,
        default=Mechanism.COUPON,
    )
    coupon = models.ForeignKey(
        Coupon,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="offers",
        help_text=(
            "Required for a coupon offer. The card shows this coupon's code and discount. "
            "Only coupons open to everyone can be used (no assigned users)."
        ),
    )

    starts_at = models.DateTimeField(help_text="When the card appears. Times are UTC (UK summer time is UTC+1).")
    ends_at = models.DateTimeField(help_text="When the card disappears. Times are UTC (UK summer time is UTC+1).")

    cta_label = models.CharField("button text", max_length=40, blank=True, help_text='Empty = "Choose a plan".')
    cta_href = models.CharField(
        "button link",
        max_length=255,
        blank=True,
        validators=[validate_internal_link],
        help_text="A link inside the site, for example /plans. Empty = /plans.",
    )

    priority = models.PositiveIntegerField(default=100, help_text="Lower numbers show first.")
    is_published = models.BooleanField(
        default=False,
        help_text="Only published offers are shown, and only between the start and end times.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )

    class Meta:
        ordering = ["priority", "ends_at", "pk"]
        indexes = [models.Index(fields=["is_published", "starts_at", "ends_at"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")),
                name="offer_ends_after_start",
                violation_error_message="The end time must be after the start time.",
            ),
        ]

    def __str__(self):
        return self.title

    def clean(self):
        errors = {}

        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            errors["ends_at"] = "The end time must be after the start time."

        if not isinstance(self.bullets, list) or not all(isinstance(b, str) for b in self.bullets):
            errors["bullets"] = "Bullets must be a list of text."
        elif len(self.bullets) > MAX_BULLETS:
            errors["bullets"] = f"Use at most {MAX_BULLETS} bullet points."

        if self.mechanism == self.Mechanism.COUPON:
            if self.coupon_id is None:
                errors["coupon"] = "Choose the coupon this offer shows."
            else:
                errors.update(self._coupon_errors())
        elif self.coupon_id is not None:
            errors["coupon"] = "An information-only offer has no coupon. Remove it or change the type."

        if errors:
            raise ValidationError(errors)

    def _coupon_errors(self):
        coupon = self.coupon
        if coupon.users.exists():
            # A personal or staff code must never be shown to everyone.
            return {"coupon": "This coupon is assigned to particular users, so it can't be shown on the offers page."}
        if self.starts_at and coupon.valid_from and self.starts_at < coupon.valid_from:
            return {"starts_at": "The offer can't start before the coupon is valid (coupon valid from %s)."
                    % coupon.valid_from.strftime("%d %b %Y %H:%M UTC")}
        if self.ends_at and coupon.valid_till and self.ends_at > coupon.valid_till:
            return {"ends_at": "The offer can't end after the coupon stops working (coupon valid till %s)."
                    % coupon.valid_till.strftime("%d %b %Y %H:%M UTC")}
        return {}
