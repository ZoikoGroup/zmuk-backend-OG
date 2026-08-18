"""Email fulfilment details to the customer after activation.

Resilient send (same approach as the sim_orders app): try real SMTP, and if the
mailbox rejects it, print the details to the runserver terminal so the dev flow
still works.

Two SIM types, two emails:
- eSIM: the QR / LPA profile (`send_esim_qr_email`), delivered three ways for
  maximum client compatibility — an inline <img> (data URL), a PNG attachment
  decoded from that data URL, and the raw LPA string as text.
- pSIM: the physical SIM's ICCID / IMSI / MSISDN (`send_psim_details_email`),
  since there's no QR to install — the customer needs these to identify and
  activate the physical card.
"""

from __future__ import annotations

import base64
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger("apps.sims")


def _from_email() -> str:
    return (
        getattr(settings, "DEFAULT_FROM_EMAIL", None)
        or getattr(settings, "EMAIL_HOST_USER", "")
    )


def _decode_png(data_url: str) -> bytes | None:
    """Return the raw PNG bytes from a 'data:image/png;base64,...' URL."""
    if not data_url or "base64," not in data_url:
        return None
    try:
        return base64.b64decode(data_url.split("base64,", 1)[1])
    except Exception:
        return None


def send_esim_qr_email(to_email: str, name: str, sim, order_reference: str = "") -> bool:
    """Send the eSIM QR to `to_email`. Returns True if handed to SMTP.

    `sim` is a Sim instance with the esim_qr_* fields populated.
    """
    lpa = sim.esim_qr_value or ""
    data_url = sim.esim_qr_data_url or ""
    greeting = name.strip() if name and name.strip() else "there"

    subject = "Your Zoiko eSIM is ready — scan to install"
    text = (
        f"Hello {greeting},\n\n"
        f"Your Zoiko eSIM is ready to install.\n\n"
        f"Scan the attached QR code with your phone's camera, or install the\n"
        f"eSIM manually using this activation string:\n\n"
        f"  {lpa}\n\n"
        f"SM-DP+ address: {sim.esim_smdp_address}\n"
        f"Activation code: {sim.esim_activation_code}\n"
        f"SIM (ICCID): {sim.iccid}\n"
        + (f"Order reference: {order_reference}\n" if order_reference else "")
        + "\nWarm regards,\nZoiko Support Team"
    )

    img_block = (
        f'<div style="text-align:center;margin:18px 0">'
        f'<img src="{data_url}" alt="eSIM QR code" '
        f'style="width:220px;height:220px;border:1px solid #eee;border-radius:8px"/>'
        f"</div>"
        if data_url else ""
    )
    html = f"""\
<div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
  <h2 style="color:#1E8189">Your eSIM is ready</h2>
  <p>Hello <strong>{greeting}</strong>,</p>
  <p>Scan the QR code below with your phone's camera to install your Zoiko eSIM.</p>
  {img_block}
  <p>If you can't scan it, install the eSIM manually with this activation string:</p>
  <div style="word-break:break-all;background:#f7f7f7;border:1px dashed #669933;
              padding:12px;border-radius:6px;font-family:monospace">{lpa}</div>
  <p style="color:#555;font-size:13px;margin-top:14px">
    SM-DP+: {sim.esim_smdp_address}<br/>
    ICCID: {sim.iccid}
    {("<br/>Order: " + order_reference) if order_reference else ""}
  </p>
  <p>Warm regards,<br/>Zoiko Support Team</p>
</div>"""

    try:
        msg = EmailMultiAlternatives(subject, text, _from_email(), [to_email])
        msg.attach_alternative(html, "text/html")
        png = _decode_png(data_url)
        if png:
            msg.attach("zoiko-esim-qr.png", png, "image/png")
        msg.send(fail_silently=False)
        logger.info("eSIM QR emailed to %s (ICCID %s).", to_email, sim.iccid)
        print(f"[SIM] eSIM QR emailed to {to_email} (ICCID {sim.iccid}).")
        return True
    except Exception as exc:
        line = "=" * 60
        print(line)
        print("[SIM] eSIM QR email could NOT be sent — details below.")
        print(f"[SIM] Send error: {type(exc).__name__}: {exc}")
        print(f"[SIM] To:    {to_email}")
        print(f"[SIM] ICCID: {sim.iccid}")
        print(f"[SIM] LPA:   {lpa}")
        print(line)
        logger.error("eSIM QR email failed for %s: %s", to_email, exc)
        return False


def send_psim_details_email(to_email: str, name: str, sim, order_reference: str = "") -> bool:
    """Send the physical SIM's ICCID / IMSI / MSISDN to `to_email`.

    Returns True if handed to SMTP. `sim` is a Sim instance for a physical
    (pSIM) card.
    """
    greeting = name.strip() if name and name.strip() else "there"

    subject = "Your Zoiko SIM card details"
    text = (
        f"Hello {greeting},\n\n"
        f"Your Zoiko physical SIM has been activated. Here are your SIM details:\n\n"
        f"  ICCID:  {sim.iccid}\n"
        f"  IMSI:   {sim.imsi}\n"
        f"  MSISDN: {sim.msisdn}\n\n"
        + (f"Order reference: {order_reference}\n" if order_reference else "")
        + "\nWarm regards,\nZoiko Support Team"
    )

    html = f"""\
<div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
  <h2 style="color:#1E8189">Your SIM is ready</h2>
  <p>Hello <strong>{greeting}</strong>,</p>
  <p>Your Zoiko physical SIM has been activated. Here are your SIM details:</p>
  <table style="border-collapse:collapse;margin:14px 0">
    <tr><td style="padding:4px 12px 4px 0;color:#555">ICCID</td>
        <td style="font-family:monospace">{sim.iccid}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#555">IMSI</td>
        <td style="font-family:monospace">{sim.imsi}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#555">MSISDN</td>
        <td style="font-family:monospace">{sim.msisdn}</td></tr>
  </table>
  <p style="color:#555;font-size:13px;margin-top:14px">
    {("Order: " + order_reference) if order_reference else ""}
  </p>
  <p>Warm regards,<br/>Zoiko Support Team</p>
</div>"""

    try:
        msg = EmailMultiAlternatives(subject, text, _from_email(), [to_email])
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=False)
        logger.info("pSIM details emailed to %s (ICCID %s).", to_email, sim.iccid)
        print(f"[SIM] pSIM details emailed to {to_email} (ICCID {sim.iccid}).")
        return True
    except Exception as exc:
        line = "=" * 60
        print(line)
        print("[SIM] pSIM details email could NOT be sent — details below.")
        print(f"[SIM] Send error: {type(exc).__name__}: {exc}")
        print(f"[SIM] To:     {to_email}")
        print(f"[SIM] ICCID:  {sim.iccid}")
        print(f"[SIM] IMSI:   {sim.imsi}")
        print(f"[SIM] MSISDN: {sim.msisdn}")
        print(line)
        logger.error("pSIM details email failed for %s: %s", to_email, exc)
        return False
