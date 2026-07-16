from django.db import models


class StudentDiscountApplication(models.Model):

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("under_review", "Under Review"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

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
    # Upload
    # ------------------------

    student_id_document = models.FileField(
        upload_to="student-discount/"
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

    def __str__(self):
        return f"{self.full_name} ({self.selected_plan})"