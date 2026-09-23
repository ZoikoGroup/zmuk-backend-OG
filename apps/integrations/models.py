from django.db import models

class IntegrationInterest(models.Model):

    full_name = models.CharField(max_length=255)

    job_title = models.CharField(max_length=255)

    company_name = models.CharField(max_length=255)

    business_email = models.EmailField()

    phone_number = models.CharField(max_length=50)

    business_website = models.URLField(blank=True, null=True)

    integration_interests = models.JSONField(default=list)

    operating_regions = models.CharField(max_length=255)

    traveler_volume = models.CharField(max_length=100)

    distribution_channels = models.JSONField(default=list)

    business_objective = models.TextField(blank=True, null=True)

    agreed = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.full_name