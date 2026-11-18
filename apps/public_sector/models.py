from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q


MAX_NOMINEES = 5


class PublicSectorRegistration(models.Model):

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    # A user may only have one open application; after a rejection they can apply again.
    OPEN_STATUSES = (Status.PENDING, Status.APPROVED)

    # Null only for applications sent before login was required that
    # couldn't be matched to an account.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="public_sector_registrations",
    )

    # Personal details
    full_name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=20)
    date_of_birth = models.DateField()

    # Employment details
    employer_name = models.CharField(max_length=100)
    job_title = models.CharField(max_length=100)
    work_email = models.EmailField()
    department = models.CharField(max_length=100)

    # Declarations
    agree_terms = models.BooleanField(default=False)
    is_public_sector_worker = models.BooleanField(default=False)
    confirm_accurate = models.BooleanField(default=False)

    # Internal
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    email_sent = models.BooleanField(default=False)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(
        blank=True,
        help_text="Shown to the customer when the status is Rejected.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Public Sector Registration"
        verbose_name_plural = "Public Sector Registrations"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(status__in=["pending", "approved"]),
                name="public_sector_one_open_registration_per_user",
                violation_error_message=(
                    "This customer already has a pending or approved registration."
                ),
            ),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.email})"


class PublicSectorOffer(models.Model):
    """The public sector discount: a percentage off selected normal plans.

    Verified (approved) public sector workers get it automatically at
    checkout on these plans, whichever page they buy from. It can't be
    combined with a coupon. There is a single offer row.
    """

    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("30.00"),
        validators=[MinValueValidator(Decimal("0.01")), MaxValueValidator(Decimal("100.00"))],
        help_text="Percentage taken off the plan price, e.g. 30.",
    )
    plans = models.ManyToManyField(
        "plans.Plan",
        blank=True,
        related_name="public_sector_offers",
        limit_choices_to={"audience": "general"},
        help_text="The normal plans shown on the public sector page that get the discount.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Untick to pause the discount (the page then shows normal prices).",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Public sector offer"
        verbose_name_plural = "Public sector offer"

    def __str__(self):
        return f"{self.discount_percent.normalize():f}% off for verified public sector workers"


class Nominee(models.Model):

    registration = models.ForeignKey(
        PublicSectorRegistration,
        on_delete=models.CASCADE,
        related_name="nominees",
    )

    full_name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=20)
    relationship = models.CharField(max_length=50)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Nominee"
        verbose_name_plural = "Nominees"
        ordering = ["id"]

    def __str__(self):
        return f"{self.full_name} ({self.relationship})"