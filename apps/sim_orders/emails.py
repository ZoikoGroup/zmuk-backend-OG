"""Send the ZM###### activation code by email.

Resilient send (same approach as the activation app): try real SMTP, and if the
mailbox rejects it (e.g. Microsoft 365 Authenticated SMTP disabled) fall back to
printing the code in the runserver terminal so the flow still works in dev.
"""
from django.conf import settings
from django.core.mail import send_mail

from .models import ACTIVATION_CODE_TTL_HOURS


def _from_email() -> str:
    return getattr(settings, "DEFAULT_FROM_EMAIL", None) or getattr(settings, "EMAIL_HOST_USER", "")


def send_activation_code_email(order, code: str) -> None:
    subject = "Zoiko Mobile SIM Activation - Your activation code"
    text = (
        f"Hello {order.customer_name or 'there'},\n\n"
        f"Thank you for your purchase, and welcome to Zoiko Mobile!\n\n"
        f"Your SIM activation code (OTP) is: {code}\n"
        f"This code is valid for {ACTIVATION_CODE_TTL_HOURS} hours.\n\n"
        f"To activate: go to 'Activate Your SIM', enter this code together with your\n"
        f"SIM serial number and your details.\n\n"
        f"Order reference: {order.order_ref}\n\n"
        f"Warm regards,\nZoiko Support Team"
    )
    html = f"""\
<div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
  <h2 style="color:#1E8189">Welcome to Zoiko Mobile!</h2>
  <p>Hello <strong>{order.customer_name or 'there'}</strong>,</p>
  <p>Thank you for your purchase. To activate your SIM, use the code below:</p>
  <div style="text-align:center;border:1px dashed #669933;background:#f9f9f9;
              padding:15px;font-size:20px;font-weight:bold;border-radius:6px;margin:15px 0">
    {code}
  </div>
  <p>This code is valid for <strong>{ACTIVATION_CODE_TTL_HOURS} hours</strong>.</p>
  <p>Go to <strong>Activate Your SIM</strong>, then enter this code with your SIM serial
     number and your details.</p>
  <p style="color:#777">Order reference: {order.order_ref}</p>
  <p>Warm regards,<br/>Zoiko Support Team</p>
</div>"""

    try:
        send_mail(
            subject=subject,
            message=text,
            from_email=_from_email(),
            recipient_list=[order.email],
            html_message=html,
            fail_silently=False,
        )
        print(f"[SIM] Activation code emailed to {order.email} (order {order.order_ref}).")
    except Exception as exc:
        line = "=" * 60
        print(line)
        print("[SIM] Activation email could NOT be sent — printing code instead.")
        print(f"[SIM] Send error: {type(exc).__name__}: {exc}")
        print(f"[SIM] To:    {order.email}")
        print(f"[SIM] Order: {order.order_ref}")
        print(f"[SIM] Code:  {code}   (valid {ACTIVATION_CODE_TTL_HOURS}h)")
        print(line)
