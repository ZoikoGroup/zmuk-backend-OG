"""Resolve the (email, name) recipient for a SIM's fulfilment emails.

Shared by `esim.py` (QR delivery) and `psim.py` (ICCID/IMSI/MSISDN delivery)
so both send to the same place: the order's `billing_email` — the checkout's
billing-address email — falling back to `pending_contact` for SIMs whose
order row isn't resolvable yet.
"""

from __future__ import annotations


def customer_for(sim) -> tuple[str, str]:
    """(billing_email, name) for the SIM's order.

    Reads the sims.Order created at checkout (linked by order_reference) and
    uses its `billing_email` — NOT `email` — since billing contact and
    account email can differ. Falls back to `pending_contact` if the Order
    row can't be found or has no billing_email on file.
    """
    email = name = ""
    try:
        from .models import Order
        order = Order.objects.filter(order_reference=sim.order_reference).first()
        if order:
            email = order.billing_email or ""
            sub = order.subscriber or {}
            name = " ".join(
                p for p in (sub.get("first_name", ""), sub.get("last_name", "")) if p
            ).strip()
    except Exception:  # Order model absent or lookup failed — fall through
        pass

    if not email and isinstance(sim.pending_contact, dict):
        email = sim.pending_contact.get("email", "") or ""
        name = name or " ".join(
            p for p in (sim.pending_contact.get("first_name", ""),
                        sim.pending_contact.get("last_name", "")) if p
        ).strip()
    return email, name
