from django.db import models
from django.conf import settings


class SupportTicket(models.Model):
    STATUS_CHOICES = [
        ("Open", "Open"),
        ("In Progress", "In Progress"),
        ("Resolved", "Resolved"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="support_tickets",
    )
    subject = models.CharField(max_length=255)
    category = models.CharField(max_length=50, default="general")
    message = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="Open")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"#{self.id} {self.subject} ({self.status})"


class CallbackRequest(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="callback_requests",
    )
    phone = models.CharField(max_length=20)
    handled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Callback {self.phone} @ {self.created_at:%Y-%m-%d %H:%M}"
