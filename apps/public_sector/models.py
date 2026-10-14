from django.db import models


class PublicSectorRegistration(models.Model):

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Public Sector Registration"
        verbose_name_plural = "Public Sector Registrations"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.full_name} ({self.email})"


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