from django.db import models


class TravelPartner(models.Model):

    ROLE_CHOICES = [
        ("Owner", "Owner"),
        ("Manager", "Manager"),
        ("Agent", "Agent"),
        ("Other", "Other"),
    ]

    full_name = models.CharField(max_length=255)

    company_name = models.CharField(max_length=255)

    business_email = models.EmailField()

    role = models.CharField(
        max_length=50,
        choices=ROLE_CHOICES
    )

    website = models.URLField(
        blank=True,
        null=True
    )

    message = models.TextField(
        blank=True,
        null=True
    )

    consent = models.BooleanField(default=False)

    # NEW FIELD
    email_sent = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):

        return (
            f"{self.full_name} - "
            f"{self.company_name}"
        )
