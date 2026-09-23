"""eSIM QR delivery: fetch the QR/LPA profile, store it on the SIM, email it.

Used both by the `send_esim_qr` management command (the reliable, scheduled
path) and optionally inline right after activation for immediacy. Idempotent —
once a SIM's QR has been emailed it's skipped, and a not-yet-ready profile is
simply left for the next run.
"""

from __future__ import annotations

import base64
import logging

from django.core.files.base import ContentFile
from django.utils import timezone

from .contacts import customer_for
from .emails import send_esim_qr_email
from .transatel import TransatelError

logger = logging.getLogger("apps.sims")


def _decode_png(data_url: str) -> bytes | None:
    """Return the raw PNG bytes from a 'data:image/png;base64,...' URL."""
    if not data_url or "base64," not in data_url:
        return None
    try:
        return base64.b64decode(data_url.split("base64,", 1)[1])
    except Exception:
        return None


def _store_qr(sim, qr: dict) -> None:
    sim.esim_activation_code = qr.get("activation_code", "") or ""
    sim.esim_matching_id = qr.get("matching_id", "") or ""
    sim.esim_smdp_address = qr.get("smdp_address", "") or ""
    sim.esim_qr_value = qr.get("qr_code_value", "") or ""
    sim.esim_qr_data_url = qr.get("qr_code_data_url", "") or ""
    sim.esim_qr_status = qr.get("status", "") or ""
    sim.esim_qr_fetched_at = timezone.now()

    update_fields = [
        "esim_activation_code", "esim_matching_id", "esim_smdp_address",
        "esim_qr_value", "esim_qr_data_url", "esim_qr_status", "esim_qr_fetched_at",
    ]

    # Save the QR PNG itself to disk/media storage (not just the base64 text)
    # so it can be served as a plain image URL — e.g. from an order-detail
    # page — without the frontend having to carry the data URL around.
    # eSIM-only by construction: this is only ever reached for eSIMs (see
    # `deliver_qr`'s `is_esim` guard below), so pSIMs never get a QR file.
    png = _decode_png(sim.esim_qr_data_url)
    if png:
        sim.esim_qr_image.save(
            f"{sim.iccid}.png", ContentFile(png), save=False
        )
        update_fields.append("esim_qr_image")

    sim.save(update_fields=update_fields)


def deliver_qr(service, sim) -> str:
    """Fetch → store → email the eSIM QR for one SIM.

    Returns one of: "sent", "already", "not_esim", "not_ready", "no_email",
    "error:<msg>". Safe to call repeatedly; only sends once.
    """
    if sim.esim_qr_emailed:
        return "already"
    if not sim.is_esim:
        return "not_esim"

    serial = sim.serial_number or sim.iccid
    try:
        qr = service.get_esim_qr(serial)
    except TransatelError as exc:
        logger.warning("eSIM QR lookup failed for %s: %s", sim.iccid, exc)
        return f"error:{exc}"

    if not qr or not qr.get("qr_code_value"):
        return "not_ready"

    _store_qr(sim, qr)

    email, name = customer_for(sim)
    if not email:
        logger.info("QR ready for %s but no customer email on file yet.", sim.iccid)
        return "no_email"

    sent = send_esim_qr_email(email, name, sim, sim.order_reference)
    if sent:
        sim.esim_qr_emailed = True
        sim.save(update_fields=["esim_qr_emailed"])
        return "sent"
    return "error:email_send_failed"