from django.db import models


class TravelEcosystemPartnerInquiry(models.Model):

    MONTHLY_VOLUME_CHOICES = [
        ("0-1K", "0-1K"),
        ("1K-10K", "1K-10K"),
        ("10K-50K", "10K-50K"),
        ("50K-100K", "50K-100K"),
        ("100K-200K", "100K-200K"),
        ("200K+", "200K+"),
    ]

    first_name = models.CharField(max_length=255)

    last_name = models.CharField(max_length=255)

    work_email = models.EmailField()

    company_name = models.CharField(max_length=255)

    website = models.URLField(
        blank=True,
        null=True
    )

    # DYNAMIC COUNTRY STORAGE
    country_region = models.CharField(max_length=255)

    role_function = models.CharField(max_length=255)

    monthly_traveler_volume = models.CharField(
        max_length=100,
        choices=MONTHLY_VOLUME_CHOICES
    )

    partnership_interest = models.CharField(max_length=255)

    message = models.TextField()

    consent = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):

        return f"{self.first_name} {self.last_name}"