from django.db import models


class EnterpriseInquiry(models.Model):

    ROLE_CHOICES = [
        ("Owner", "Owner"),
        ("Manager", "Manager"),
        ("Agent", "Agent"),
        ("Other", "Other"),
    ]

    ENQUIRY_CHOICES = [
        ("Corporate Travel", "Corporate Travel"),
        ("Remote Workforce", "Remote Workforce"),
        ("API Integration", "API Integration"),
        ("Custom Program", "Custom Program"),
    ]

    full_name = models.CharField(max_length=255)

    company_name = models.CharField(max_length=255)

    business_email = models.EmailField()

    role = models.CharField(
        max_length=100,
        choices=ROLE_CHOICES
    )

    website = models.URLField(
        blank=True,
        null=True
    )

    phone_number = models.CharField(
        max_length=20,
        blank=True,
        null=True
    )

    enquiry_type = models.CharField(
        max_length=100,
        choices=ENQUIRY_CHOICES
    )

    message = models.TextField()

    consent = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.full_name} - {self.company_name}"