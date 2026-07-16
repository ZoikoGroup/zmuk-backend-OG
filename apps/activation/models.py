from django.db import models


class SimActivation(models.Model):
    username = models.CharField(max_length=100)
    email = models.EmailField()

    otp = models.CharField(max_length=8)
    sim_serial = models.CharField(max_length=25)

    title = models.CharField(max_length=20)

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)

    dob = models.DateField()

    zoiko_package = models.CharField(max_length=100)

    country = models.CharField(max_length=100)
    postcode = models.CharField(max_length=30)
    city = models.CharField(max_length=100)

    address = models.TextField()

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class EmailOtp(models.Model):
    """A one-time 6-digit email verification code.

    We never store the code in plaintext — only a salted hash (via Django's
    password hashers). Flow: request -> verify (sets `verified`) -> the
    activation save consumes it (`is_used`).
    """

    email = models.EmailField(db_index=True)
    code_hash = models.CharField(max_length=128)
    purpose = models.CharField(max_length=30, default="sim_activation")

    attempts = models.PositiveSmallIntegerField(default=0)
    verified = models.BooleanField(default=False)
    is_used = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["email", "purpose", "is_used"],
                name="activ_otp_lookup_idx",
            ),
        ]

    def __str__(self):
        state = "used" if self.is_used else ("verified" if self.verified else "active")
        return f"OTP {self.email} ({state})"