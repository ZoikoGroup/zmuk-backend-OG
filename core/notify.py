"""Internal alerts to the Zoiko team.

Everything here is best-effort: it logs and returns False on any problem and
NEVER raises, so a mail glitch can never break a payment or a webhook.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMessage

logger = logging.getLogger("core.notify")

_FALLBACK = "info@zoikomobile.co.uk"
_ADDRESS_FIELDS = (
    ("Company", "companyName"),
    ("Street", "street"),
    ("House no.", "houseNumber"),
    ("City", "city"),
    ("County/State", "state"),
    ("Postcode", "zip"),
    ("Country", "region"),
    ("Phone", "phone"),
    ("Email", "email"),
)


def purchase_recipients():
    """PURCHASE_NOTIFY_EMAIL (comma-separated), else the info@ mailbox."""
    raw = getattr(settings, "PURCHASE_NOTIFY_EMAIL", "") or _FALLBACK
    return [a.strip() for a in str(raw).replace(";", ",").split(",") if a.strip()]


def _clean(value):
    """One line, no control characters: keeps the email tidy and header-safe."""
    return " ".join(str(value if value is not None else "").split())


def _address_block(title, addr):
    if not isinstance(addr, dict):
        return []
    rows = []
    name = _clean(f"{addr.get('firstName', '')} {addr.get('lastName', '')}")
    if name:
        rows.append(f"  Name: {name}")
    for label, key in _ADDRESS_FIELDS:
        val = _clean(addr.get(key))
        if val:
            rows.append(f"  {label}: {val}")
    return [f"{title}:", *rows, ""] if rows else []


def notify_team_of_purchase(kind, order_ref, customer_email, total,
                            items=None, billing=None, shipping=None, notes=None):
    """Email the team that a customer has paid. Returns True if it was sent."""
    try:
        lines = [
            "A customer has paid for an order on the Zoiko Mobile website.",
            "",
            f"Type: {_clean(kind)}",
            f"Order reference: {_clean(order_ref)}",
            f"Total paid: {_clean(total)}",
            f"Customer email: {_clean(customer_email)}",
            "",
        ]
        if items:
            lines.append("Items:")
            lines += [f"  - {_clean(i)}" for i in items]
            lines.append("")
        lines += _address_block("Billing details", billing)
        lines += _address_block("Delivery details", shipping)
        if notes:
            lines += [_clean(n) for n in notes] + [""]
        lines.append("This is an automatic notification from the Zoiko Mobile backend.")

        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@zoikomobile.co.uk"
        EmailMessage(
            subject=f"New paid order {_clean(order_ref)} - {_clean(kind)}",
            body="\n".join(lines),
            from_email=from_email,
            to=purchase_recipients(),
        ).send(fail_silently=False)
        logger.info("Purchase alert sent for %s", _clean(order_ref))
        return True
    except Exception:
        logger.exception("Could not send purchase alert for %s", order_ref)
        return False
