from django.db import models


class EnterpriseInquiry(models.Model):

    first_name = models.CharField(max_length=100)

    last_name = models.CharField(max_length=100)

    business_email = models.EmailField()

    company_name = models.CharField(max_length=255)

    phone = models.CharField(
        max_length=20,
        blank=True,
        null=True
    )

    country = models.CharField(max_length=100)

    inquiry_type = models.CharField(max_length=255)

    message = models.TextField(
        blank=True,
        null=True
    )

    consent = models.BooleanField(default=False)

    # NEW FIELD
    email_sent = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Enterprise"
        verbose_name_plural = "Enterprise"

    def __str__(self):

        return (
            f"{self.first_name} "
            f"{self.last_name}"
        )