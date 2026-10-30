from django.db import models
from django.contrib.auth.models import User


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    bq_enrollment_id = models.CharField(max_length=100, blank=True, null=True)

    # Customer-editable details (PUT /api/accounts/update-profile/)
    phone_number = models.CharField(max_length=20, blank=True, default="")
    preferred_name = models.CharField(max_length=50, blank=True, default="")
    billing_address = models.TextField(max_length=300, blank=True, default="")
    # Empty means "same as billing".
    delivery_address = models.TextField(max_length=300, blank=True, default="")
    # {"contact_method": ..., "accessibility": ..., "support_needs": ...}
    # Keys are whitelisted by UpdateUserSerializer.
    preferences = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.user.username
