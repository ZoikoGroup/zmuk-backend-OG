"""Milestone 4, step B1: the secure continuation link.

A request that ops mark READY_FOR_ORDER gets a single-use link emailed to
the address on the request. The customer opens it, signs in (or creates an
account) and claims the request, which attaches it to that account. Only
then can they review documents and pay (B2–B4).

Why this is safe (decisions A1, M04 §04 and the 13 Nov engineering note):
* The link only goes to the request's own email, so using it proves control
  of that inbox. Typing an email into a form proves nothing, so requests are
  never attached by email matching.
* Only a SHA-256 of the token is stored; the link is single use, expires
  (SWITCH_CONTINUATION_TTL_HOURS) and a new link revokes older ones.
* A request already owned by another account is never moved.

Everything here is off while SWITCH_ORDER_CONVERSION_ENABLED is False, and
also while ADD_LINE_SUPPLIER_WRITES_ENABLED is False (see `enabled`).
"""

import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import services
from .emails import send_html_email
from .models import SwitchContinuationToken, SwitchRequest, SwitchStatusEvent

S = SwitchRequest.Status


class ContinuationError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


NOT_AVAILABLE = ContinuationError(
    "NOT_AVAILABLE", "Completing your order online isn't available yet. We'll contact you.", 404
)
INVALID_LINK = ContinuationError(
    "INVALID_LINK", "This link isn't valid. Please use the latest link we emailed you.", 404
)
LINK_EXPIRED = ContinuationError(
    "LINK_EXPIRED", "This link has expired. You can ask us to send a new one.", 410
)
LINK_USED = ContinuationError(
    "LINK_USED", "This link has already been used. Sign in to continue, or ask for a new link.", 410
)
NOT_READY = ContinuationError(
    "REQUEST_NOT_READY", "Your request isn't ready for the next step yet. We'll email you when it is.", 409
)
OTHER_ACCOUNT = ContinuationError(
    "REQUEST_BELONGS_TO_ANOTHER_ACCOUNT",
    "This request is already linked to a different account. Please contact support.", 409,
)
EMAIL_NOT_VERIFIED = ContinuationError(
    "EMAIL_NOT_VERIFIED", "Please verify your account's email address, then try again.", 403
)

RESEND_MESSAGE = (
    "If that request is ready for the next step, we've emailed a new link to the "
    "address on the request."
)


def conversion_flag_on() -> bool:
    return bool(getattr(settings, "SWITCH_ORDER_CONVERSION_ENABLED", False))


def supplier_writes_on() -> bool:
    return bool(getattr(settings, "ADD_LINE_SUPPLIER_WRITES_ENABLED", False))


def enabled() -> bool:
    """Part B is open only when conversion is on AND the line can really be
    activated (supplier writes on), unless Finance approved a time-limited
    exception. Conversion ON with supplier writes OFF would take payment for
    service that can't be provisioned (Engineering Decision v2.1 §04/§07)."""
    if not conversion_flag_on():
        return False
    return supplier_writes_on() or bool(
        getattr(settings, "SWITCH_ORDERS_WITHOUT_SUPPLIER_WRITES_APPROVED", False)
    )


def disabled_reason() -> str:
    """For staff messages only."""
    if not conversion_flag_on():
        return "SWITCH_ORDER_CONVERSION_ENABLED is off"
    if not enabled():
        return ("ADD_LINE_SUPPLIER_WRITES_ENABLED is off (payment is never taken for a line that "
                "can't be activated)")
    return ""


def _hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def continuation_url(raw_token: str) -> str:
    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    path = "/" + settings.SWITCH_CONTINUATION_PATH.strip("/")
    return f"{base}{path}?{urlencode({'token': raw_token})}"


def _event(switch, kind, actor=None, note=""):
    SwitchStatusEvent.objects.create(
        switch_request=switch, kind=kind, status_before=switch.status, status_after=switch.status,
        actor=actor if actor is not None and getattr(actor, "is_authenticated", False) else None,
        note=note,
    )


# ── Issuing ───────────────────────────────────────────────────────────────────

def issue_link(switch: SwitchRequest, actor=None) -> SwitchContinuationToken:
    """Revoke older links, create a new one and email it. Raises
    ContinuationError if the feature is off or the request isn't ready."""
    if not enabled():
        raise NOT_AVAILABLE
    if switch.status != S.READY_FOR_ORDER:
        raise NOT_READY

    raw = secrets.token_urlsafe(32)
    now = timezone.now()
    with transaction.atomic():
        SwitchContinuationToken.objects.filter(
            switch_request=switch, used_at__isnull=True, revoked_at__isnull=True,
        ).update(revoked_at=now)
        token = SwitchContinuationToken.objects.create(
            switch_request=switch,
            token_hash=_hash(raw),
            sent_to=services.mask_email(switch.email),
            expires_at=now + timedelta(hours=settings.SWITCH_CONTINUATION_TTL_HOURS),
            created_by=actor if actor is not None and getattr(actor, "is_authenticated", False) else None,
        )

    delivered = send_html_email(
        subject=f"Continue your Zoiko Mobile switch {switch.reference}",
        template_name="emails/switch_continue.html",
        context={
            "first_name": switch.first_name,
            "reference": switch.reference,
            "selected_plan": switch.selected_plan,
            "continue_url": continuation_url(raw),
            "ttl_hours": settings.SWITCH_CONTINUATION_TTL_HOURS,
        },
        recipient=switch.email,
    )
    if delivered:
        token.delivered = True
        token.save(update_fields=["delivered"])
    _event(switch, SwitchStatusEvent.Kind.LINK_SENT, actor=actor,
           note="emailed" if delivered else "email failed")
    return token


def resend_link(reference: str, email: str) -> None:
    """Customer asked for a new link. Sends only when the reference and email
    match a ready request; the caller always shows RESEND_MESSAGE."""
    if not enabled():
        raise NOT_AVAILABLE
    switch = SwitchRequest.objects.filter(reference=(reference or "").strip().upper()).first()
    if (
        switch is None
        or switch.status != S.READY_FOR_ORDER
        or (email or "").strip().lower() != switch.email.strip().lower()
    ):
        return
    issue_link(switch)


# ── Using ─────────────────────────────────────────────────────────────────────

def _lookup(raw_token: str, *, for_update=False) -> SwitchContinuationToken:
    raw = (raw_token or "").strip()
    if not raw or len(raw) > 200:
        raise INVALID_LINK
    qs = SwitchContinuationToken.objects.select_related("switch_request")
    if for_update:
        qs = qs.select_for_update()
    token = qs.filter(token_hash=_hash(raw)).first()
    if token is None or token.revoked_at:
        raise INVALID_LINK
    return token


def check_link(raw_token: str, user=None) -> tuple[SwitchRequest, bool]:
    """(request, already_claimed_by_user). Raises ContinuationError."""
    if not enabled():
        raise NOT_AVAILABLE
    token = _lookup(raw_token)
    switch = token.switch_request
    signed_in = user is not None and user.is_authenticated
    if token.used_at:
        if signed_in and token.used_by_id == user.id and switch.user_id == user.id:
            return switch, True
        raise LINK_USED
    if token.expires_at <= timezone.now():
        raise LINK_EXPIRED
    if switch.status != S.READY_FOR_ORDER:
        raise NOT_READY
    return switch, bool(signed_in and switch.user_id == user.id)


def claim(raw_token: str, user) -> SwitchRequest:
    """Attach the request to the signed-in `user` and use up the link.

    Repeating the call with the same link and account is harmless.
    """
    if not enabled():
        raise NOT_AVAILABLE
    if not (user.is_active and user.email):
        raise EMAIL_NOT_VERIFIED

    with transaction.atomic():
        token = _lookup(raw_token, for_update=True)
        switch = SwitchRequest.objects.select_for_update().get(pk=token.switch_request_id)
        if token.used_at:
            if token.used_by_id == user.id and switch.user_id == user.id:
                return switch
            raise LINK_USED
        if token.expires_at <= timezone.now():
            raise LINK_EXPIRED
        if switch.status != S.READY_FOR_ORDER:
            raise NOT_READY
        if switch.user_id and switch.user_id != user.id:
            raise OTHER_ACCOUNT

        now = timezone.now()
        token.used_at, token.used_by = now, user
        token.save(update_fields=["used_at", "used_by"])
        # Any other unused link for this request is no longer needed.
        SwitchContinuationToken.objects.filter(
            switch_request=switch, used_at__isnull=True, revoked_at__isnull=True,
        ).update(revoked_at=now)

        switch.user = user
        switch.claimed_at = now
        switch.save(update_fields=["user", "claimed_at", "updated_at"])
        _event(switch, SwitchStatusEvent.Kind.CLAIMED, actor=user)
    return switch
