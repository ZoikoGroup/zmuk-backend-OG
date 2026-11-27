from django.db import models
from django.conf import settings


class UserSecurity(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="security",
    )
    two_fa_enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} • 2FA={'on' if self.two_fa_enabled else 'off'}"


class AccountSecurityEvent(models.Model):
    """A security-relevant change to an account (CT-005 CTO response §7, §10.1).

    Recorded automatically by `apps.security.signals` whenever the password,
    email, phone number or an address changes, whichever code path saved it.
    Only the fact and time of the change are kept, never the old or new value.
    Risk checks (e.g. SIM type change) read these through
    `apps.security.services.recent_changes`.
    """

    class Kind(models.TextChoices):
        PASSWORD_CHANGED = "password_changed", "Password changed"
        EMAIL_CHANGED = "email_changed", "Email changed"
        PHONE_CHANGED = "phone_changed", "Phone number changed"
        BILLING_ADDRESS_CHANGED = "billing_address_changed", "Billing address changed"
        DELIVERY_ADDRESS_CHANGED = "delivery_address_changed", "Delivery address changed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="security_events",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["user", "occurred_at"])]

    def __str__(self):
        return f"{self.user} · {self.get_kind_display()} @ {self.occurred_at:%Y-%m-%d %H:%M}"


class SuspiciousReport(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="suspicious_reports",
    )
    note = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Report by {self.user} @ {self.created_at:%Y-%m-%d %H:%M}"
