from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from .storage import private_document_storage, student_document_path


class StudentOffer(models.Model):
    """The student discount: a percentage off every student plan.

    Student plans are the plans with Audience = Students (shown on the
    student page only). Anyone can buy them at the normal price; verified
    (approved, not expired) students get this % off automatically at
    checkout. It can't be combined with a coupon. There is a single row.
    """

    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("20.00"),
        validators=[MinValueValidator(Decimal("0.01")), MaxValueValidator(Decimal("100.00"))],
        help_text="Percentage taken off every student plan, e.g. 20.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Untick to pause the discount (student plans then sell at their normal price).",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Student offer"
        verbose_name_plural = "Student offer"

    def __str__(self):
        return f"{self.discount_percent.normalize():f}% off student plans for verified students"


class StudentDiscountApplication(models.Model):

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("under_review", "Under Review"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        # Set automatically once an approved student's graduation date has passed.
        ("expired", "Expired"),
    ]

    # A user may only have one open application; after a rejection or
    # expiry they can apply again.
    OPEN_STATUSES = ("pending", "under_review", "approved")

    # Null only for applications sent before login was required that
    # couldn't be matched to an account.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="student_discount_applications",
    )

    # ------------------------
    # Personal Information
    # ------------------------

    full_name = models.CharField(max_length=255)

    dob = models.DateField()

    email = models.EmailField()

    mobile = models.CharField(max_length=20)

    # ------------------------
    # Education
    # ------------------------

    institution = models.CharField(max_length=255)

    student_id_number = models.CharField(max_length=100)

    enrolment_status = models.CharField(max_length=150)

    graduation_date = models.DateField()

    # ------------------------
    # Plan
    # ------------------------

    selected_plan = models.CharField(max_length=255)

    contract_duration = models.CharField(max_length=100)

    # ------------------------
    # Features
    # ------------------------

    roaming = models.BooleanField(default=False)

    wifi_calling = models.BooleanField(default=False)

    esim = models.BooleanField(default=False)

    # ------------------------
    # Upload (private: never served by URL, admins open it via the admin)
    # ------------------------

    student_id_document = models.FileField(
        upload_to=student_document_path,
        storage=private_document_storage,
    )

    # ------------------------
    # Declaration
    # ------------------------

    signature = models.CharField(max_length=255)

    declaration_date = models.DateField()

    # ------------------------
    # Status
    # ------------------------

    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default="pending",
    )

    reviewed_at = models.DateTimeField(null=True, blank=True)

    rejection_reason = models.TextField(
        blank=True,
        help_text="Shown to the student when the status is Rejected.",
    )

    # ------------------------
    # Timestamps
    # ------------------------

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Student Discount Application"
        verbose_name_plural = "Student Discount Applications"
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(status__in=["pending", "under_review", "approved"]),
                name="student_discount_one_open_application_per_user",
                violation_error_message=(
                    "This student already has an open (pending, under review "
                    "or approved) application."
                ),
            ),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.selected_plan})"
