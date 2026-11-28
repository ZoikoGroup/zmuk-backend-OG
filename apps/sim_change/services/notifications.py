"""Customer alerts for a SIM type change (CTO v2.0 §10.1, Addendum v2.1 §4.4).

Sent to the account email when a case:
* is received (VERIFY_REQUIRED) — "didn't ask for this?";
* is about to be swapped at the supplier (PROVIDER_PENDING) — pre-swap alert;
* is confirmed by the supplier (SUPPLIER_BINDING_CONFIRMED) — post-swap alert;
* is paused for security (HOLD_FRAUD).

`transitions.transition` schedules these after the database change commits.
Emails contain the masked number only — never ICCIDs, IMSIs or activation
codes — and every alert except "paused" carries a "This wasn't me" link.

The link holds a signed, time-limited token (case + account) and works
without logging in, because the customer's session may be the compromised
one. Following it runs the same report as the in-app button.

Limitation to note for Security: we email the current account address. A
recent email change already makes the request high-risk (second approver);
alerting the previous address would need it to be kept, which it isn't today.
"""

import logging
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from ..models import SimTypeChangeCase, SimTypeChangeEvent, StatusSource
from .presenter import COPY, mask_msisdn
from .transitions import TransitionError

logger = logging.getLogger("apps.sim_change")
S = SimTypeChangeCase.Status
SALT = "sim_change.report_unauthorised"

NOTICES = {
    S.VERIFY_REQUIRED: {
        "kind": "received",
        "subject": "We've received your SIM change request",
        "heading": "We've received your SIM change request",
        "body": [COPY["accepted"]],
        "report": True,
    },
    S.PROVIDER_PENDING: {
        "kind": "pre_swap",
        "subject": "Your SIM change is about to be processed",
        "heading": "Your SIM change is about to be processed",
        "body": [COPY["in_progress"]],
        "report": True,
    },
    S.SUPPLIER_BINDING_CONFIRMED: {
        "kind": "completed",
        "subject": "Your Zoiko Mobile service has moved to your new SIM",
        "heading": "Your service has moved to your new SIM",
        "body": [COPY["proven"]],
        "report": True,
    },
    S.HOLD_FRAUD: {
        "kind": "paused",
        "subject": "We've paused your SIM change request",
        "heading": "We've paused your SIM change request",
        "body": [COPY["fraud"]],
        "report": False,
    },
}


def link_days() -> int:
    return int(getattr(settings, "SIM_CHANGE_REPORT_LINK_DAYS", 14))


def make_report_token(case: SimTypeChangeCase) -> str:
    return signing.dumps({"c": str(case.uuid), "u": case.owner_id}, salt=SALT, compress=True)


def report_url(case: SimTypeChangeCase) -> str:
    base = str(getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    path = getattr(settings, "SIM_CHANGE_REPORT_PATH", "/sim-change/report")
    return f"{base}{path}?{urlencode({'token': make_report_token(case)})}"


def case_from_token(token: str) -> SimTypeChangeCase:
    """The case a 'This wasn't me' link refers to; raises TransitionError if invalid or expired."""
    try:
        data = signing.loads(str(token or ""), salt=SALT, max_age=link_days() * 86400)
    except signing.SignatureExpired as exc:
        raise TransitionError("LINK_EXPIRED", "This link has expired. Please contact our security support team.",
                              410) from exc
    except signing.BadSignature as exc:
        raise TransitionError("INVALID_LINK", "This link isn't valid.", 400) from exc
    case = SimTypeChangeCase.objects.filter(uuid=data.get("c"), owner_id=data.get("u")).first()
    if case is None:
        raise TransitionError("INVALID_LINK", "This link isn't valid.", 400)
    return case


def mask_email(email: str) -> str:
    local, _, domain = str(email or "").partition("@")
    if not domain:
        return ""
    return f"{local[:1]}***@{domain}"


def _deliver(subject, context, recipient) -> bool:
    try:
        html = render_to_string("emails/sim_change_notice.html", context)
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@zoikomobile.co.uk"
        msg = EmailMultiAlternatives(subject=subject, body=strip_tags(html), from_email=from_email, to=[recipient])
        msg.attach_alternative(html, "text/html")
        msg.send()
        return True
    except Exception:
        logger.exception("SIM change email '%s' failed (recipient not logged)", subject)
        return False


def send(case_id: int, status: str) -> bool:
    """Send the alert for `status` (called after commit). Records the outcome on the case."""
    notice = NOTICES.get(status)
    case = SimTypeChangeCase.objects.select_related("owner", "line").filter(pk=case_id).first()
    if notice is None or case is None:
        return False
    recipient = (case.owner.email or "").strip()
    delivered = False
    if recipient:
        delivered = _deliver(notice["subject"], {
            "first_name": case.owner.first_name or "there",
            "heading": notice["heading"],
            "body": notice["body"],
            "msisdn_masked": mask_msisdn(case.line.current_msisdn),
            "direction": case.get_direction_display(),
            "reference": case.uuid.hex[:8].upper(),
            "report_url": report_url(case) if notice["report"] else "",
            "report_days": link_days(),
        }, recipient)
    SimTypeChangeEvent.objects.create(
        tenant=case.tenant, case=case, kind="notification_sent" if delivered else "notification_failed",
        status_source=StatusSource.SYSTEM, version=case.version,
        detail={"notice": notice["kind"], "to": mask_email(recipient), "reason": "" if recipient else "no email"},
    )
    return delivered


def report_from_link(token: str, note: str = "") -> SimTypeChangeCase:
    """'This wasn't me' from an email link (no login needed)."""
    from . import requests

    case = case_from_token(token)
    return requests.report_unauthorised(case.owner, case, note=f"Reported from the email link. {note}".strip())
