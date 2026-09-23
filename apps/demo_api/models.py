from django.db import models

class RequestDemo(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField()
    company = models.CharField(max_length=255, blank=True, null=True)
    org_type = models.CharField(max_length=255)
    deployment_size = models.CharField(max_length=50)
    updates = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name} - {self.email}"