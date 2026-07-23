import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import make_password, check_password
from django.core.mail import send_mail
from django.utils import timezone

from .models import EmailOtp

# --- tunables ---
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_PURPOSE_ACTIVATION = "sim_activation"


def generate_otp_code() -> str:
    """Cryptographically secure 6-digit code (000000-999999).

    Uses `secrets`, not `random` — `random` is predictable and unsafe for OTPs.
    """
    return f"{secrets.randbelow(10 ** 6):06d}"


def _from_email() -> str:
    return getattr(settings, "DEFAULT_FROM_EMAIL", None) or settings.EMAIL_HOST_USER


def _print_to_console(email: str, code: str, reason: str = "") -> None:
    """Fallback: print the OTP to the runserver terminal so the flow still works
    when the SMTP server rejects the login (e.g. M365 Authenticated SMTP disabled).
    """
    line = "=" * 60
    print(line)
    print("[OTP] Real email could NOT be sent — printing code instead.")
    if reason:
        print(f"[OTP] Send error: {reason}")
    print(f"[OTP] To:   {email}")
    print(f"[OTP] Code: {code}   (valid {OTP_TTL_MINUTES} min)")
    print(line)


def can_resend(email: str, purpose: str = OTP_PURPOSE_ACTIVATION) -> bool:
    """Rate-limit: block a new code within the cooldown window."""
    last = (
        EmailOtp.objects
        .filter(email=email, purpose=purpose)
        .order_by("-created_at")
        .first()
    )
    if last is None:
        return True
    elapsed = (timezone.now() - last.created_at).total_seconds()
    return elapsed >= OTP_RESEND_COOLDOWN_SECONDS


def create_and_send_otp(email: str, purpose: str = OTP_PURPOSE_ACTIVATION) -> EmailOtp:
    """Generate a code, store its hash, and deliver it.

    Delivery strategy:
      1. Try to send a REAL email via the configured SMTP credentials.
      2. If the SMTP server rejects/blocks the send (e.g. Microsoft 365
         "Authenticated SMTP" is off, or an app password is required), do NOT
         crash the request — fall back to printing the code in the terminal so
         the activation flow keeps working. The moment SMTP auth is enabled on
         the mailbox, real email starts sending with no code changes.
    """
    code = generate_otp_code()

    otp = EmailOtp.objects.create(
        email=email,
        purpose=purpose,
        code_hash=make_password(code),
        expires_at=timezone.now() + timedelta(minutes=OTP_TTL_MINUTES),
    )

    try:
        send_mail(
            subject="Your Zoiko Mobile verification code",
            message=(
                f"Your verification code is: {code}\n\n"
                f"This code expires in {OTP_TTL_MINUTES} minutes.\n"
                f"If you didn't request this, you can safely ignore this email."
            ),
            from_email=_from_email(),
            recipient_list=[email],
            fail_silently=False,
        )
        print(f"[OTP] Email sent to {email} via SMTP.")
    except Exception as exc:
        # SMTP refused (most likely M365 SMTP-AUTH disabled). Keep the flow alive.
        _print_to_console(email, code, reason=f"{type(exc).__name__}: {exc}")

    return otp


def _latest_active_otp(email: str, purpose: str):
    return (
        EmailOtp.objects
        .filter(
            email=email,
            purpose=purpose,
            is_used=False,
            expires_at__gt=timezone.now(),
        )
        .order_by("-created_at")
        .first()
    )


def verify_otp(email: str, code: str, purpose: str = OTP_PURPOSE_ACTIVATION):
    """Check a code. On success marks `verified` (does NOT consume it).

    Returns (ok: bool, message: str).
    """
    otp = _latest_active_otp(email, purpose)
    if otp is None:
        return False, "No active code. Please request a new one."

    if otp.attempts >= OTP_MAX_ATTEMPTS:
        return False, "Too many incorrect attempts. Please request a new code."

    if not check_password(code, otp.code_hash):
        otp.attempts += 1
        otp.save(update_fields=["attempts"])
        return False, "Incorrect code. Please try again."

    otp.verified = True
    otp.save(update_fields=["verified"])
    return True, "Email verified."


def consume_verified_otp(email: str, purpose: str = OTP_PURPOSE_ACTIVATION) -> bool:
    """Require a verified, unexpired, unused code and consume it.

    Called from the activation save so the OTP is enforced server-side even if
    the frontend is bypassed.
    """
    otp = (
        EmailOtp.objects
        .filter(
            email=email,
            purpose=purpose,
            verified=True,
            is_used=False,
            expires_at__gt=timezone.now(),
        )
        .order_by("-created_at")
        .first()
    )
    if otp is None:
        return False

    otp.is_used = True
    otp.save(update_fields=["is_used"])
    return True