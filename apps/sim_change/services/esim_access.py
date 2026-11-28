"""Secure eSIM setup for a SIM type change (CTO Addendum v2.1 A2).

UK Transatel/Auriga sequence: the swap is executed, a supplier read proves the
binding (SUPPLIER_BINDING_CONFIRMED), and only then is install access given.

1. `issue_access` (Customer Security Support Agent): after a fresh
   independent check with the customer, confirm with Transatel that the
   profile is ready and issue a short-lived, single-use access grant
   (StepUpGrant, purpose ESIM_INSTALL_ACCESS). eSIM track → PROFILE_AVAILABLE →
   ACCESS_READY; the case → SERVICE_READY (supplier binding confirmed and the
   setup path ready — not proof the handset connected).
2. `open_access` (customer): consumes the grant, fetches the installation
   details live from Transatel and returns them once. Nothing is stored,
   logged or emailed. eSIM track → ACCESS_ISSUED. Opening is not proof of
   installation.
3. `report_installed` (customer) / `verify_installation` (staff).
4. Recovery: `record_exception` (expired, consumed, lost profile…). Access can
   then be issued again only with the reference of a controlled supplier
   operation (e.g. a new Swap eSIM) — never a casual re-issue.

Gated by `SIM_CHANGE_ESIM_ACCESS_ENABLED` (off) and, while piloting, the
`SIM_CHANGE_ESIM_PILOT_USER_IDS` allowlist. The allowlist never bypasses the
other checks.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import EsimFulfilment, SimTypeChangeCase, SimTypeChangeEvent, StatusSource, StepUpGrant
from . import supplier
from .operations import require
from .transitions import TransitionError, check_step_up, set_fulfilment, transition

S = SimTypeChangeCase.Status
E = EsimFulfilment
BINDING_PROVEN = (S.SUPPLIER_BINDING_CONFIRMED, S.SERVICE_READY)


def access_minutes() -> int:
    return int(getattr(settings, "SIM_CHANGE_ESIM_ACCESS_MINUTES", 15))


def feature_open_for(user) -> bool:
    if not getattr(settings, "SIM_CHANGE_ESIM_ACCESS_ENABLED", False):
        return False
    pilot = {s.strip() for s in str(getattr(settings, "SIM_CHANGE_ESIM_PILOT_USER_IDS", "")).split(",") if s.strip()}
    return not pilot or str(user.pk) in pilot


def valid_access_grant(case) -> StepUpGrant | None:
    return case.step_up_grants.filter(
        purpose=StepUpGrant.Purpose.ESIM_INSTALL_ACCESS, assurance_result=StepUpGrant.Assurance.PASSED,
        consumed_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now(),
    ).order_by("-issued_at").first()


def _require_esim_case(case):
    if case.to_type != "esim":
        raise TransitionError("NOT_ESIM", "This request isn't for an eSIM.", 409)
    if case.status not in BINDING_PROVEN:
        raise TransitionError("BINDING_NOT_CONFIRMED", "eSIM setup opens only after the supplier confirms the swap.", 409)


# ── Staff ─────────────────────────────────────────────────────────────────

def issue_access(case, *, agent, policy_version: str, external_reference: str = "",
                 supplier_operation_reference: str = "") -> StepUpGrant:
    require(agent, "verify")
    case.refresh_from_db()  # decide on the current state, not a stale copy
    _require_esim_case(case)
    if not policy_version.strip():
        raise TransitionError("POLICY_VERSION_REQUIRED", "Record which verification procedure version was used.", 422)
    reissue_after_exception = case.esim_fulfilment == E.EXCEPTION
    if reissue_after_exception and not supplier_operation_reference.strip():
        raise TransitionError(
            "SUPPLIER_OPERATION_REQUIRED",
            "Re-issuing after a problem needs the reference of the controlled supplier operation (e.g. new Swap eSIM).",
            422,
        )
    if case.esim_fulfilment not in (E.NOT_STARTED, E.EXCEPTION, E.ACCESS_READY):
        raise TransitionError("ACCESS_NOT_AVAILABLE", "eSIM setup access can't be issued at this stage.", 409)
    if case.esim_fulfilment == E.ACCESS_READY and valid_access_grant(case) is not None:
        raise TransitionError("ACCESS_ALREADY_READY", "The customer already has valid setup access.", 409)

    try:
        supplier.esim_profile(case)  # readiness only; nothing kept
    except supplier.ProfileUnavailable as exc:
        raise TransitionError("PROFILE_NOT_READY", "The eSIM profile isn't available from the supplier yet.", 409) from exc

    with transaction.atomic():
        grant = StepUpGrant.objects.create(
            tenant=case.tenant, case=case, line=case.line, subject=case.owner,
            purpose=StepUpGrant.Purpose.ESIM_INSTALL_ACCESS,
            verification_method=StepUpGrant.Method.SUPERVISED_SUPPORT, assurance_result=StepUpGrant.Assurance.PASSED,
            policy_version=policy_version.strip()[:32], external_reference=external_reference.strip()[:128],
            verified_by=agent, expires_at=timezone.now() + timedelta(minutes=access_minutes()),
        )
        if case.esim_fulfilment != E.ACCESS_READY:
            case = set_fulfilment(case, "esim", E.PROFILE_AVAILABLE, source=StatusSource.SUPPORT, actor=agent,
                                  actor_role="security_support_agent",
                                  detail={"supplier_operation": supplier_operation_reference.strip()[:128]}
                                  if reissue_after_exception else None)
            case = set_fulfilment(case, "esim", E.ACCESS_READY, source=StatusSource.SUPPORT, actor=agent,
                                  actor_role="security_support_agent")
        if case.status == S.SUPPLIER_BINDING_CONFIRMED:
            transition(case, S.SERVICE_READY, source=StatusSource.SYSTEM,
                       reason="supplier binding confirmed and secure eSIM setup ready")
        SimTypeChangeEvent.objects.create(
            tenant=case.tenant, case=case, actor=agent, actor_role="security_support_agent",
            kind="esim_access_granted", status_source=StatusSource.SUPPORT, version=case.version,
            detail={"grant": str(grant.uuid), "expires_minutes": access_minutes()},
        )
    return grant


def record_exception(case, *, actor, reason: str) -> SimTypeChangeCase:
    require(actor, "fulfil")
    if not reason.strip():
        raise TransitionError("REASON_REQUIRED", "Say what went wrong with the eSIM setup.", 422)
    with transaction.atomic():
        case.step_up_grants.filter(
            purpose=StepUpGrant.Purpose.ESIM_INSTALL_ACCESS, consumed_at__isnull=True, revoked_at__isnull=True,
        ).update(revoked_at=timezone.now(), revoke_reason="esim setup exception")
        return set_fulfilment(case, "esim", E.EXCEPTION, source=StatusSource.SUPPORT, actor=actor, reason=reason)


def verify_installation(case, *, actor, note: str) -> SimTypeChangeCase:
    require(actor, "fulfil")
    if not note.strip():
        raise TransitionError("NOTE_REQUIRED", "Record how the installation was confirmed (e.g. test call).", 422)
    return set_fulfilment(case, "esim", E.INSTALLATION_VERIFIED, source=StatusSource.SUPPORT, actor=actor,
                          reason=note)


# ── Customer ──────────────────────────────────────────────────────────────

def open_access(user, case) -> dict:
    """Consume the access grant and return the installation details once."""
    if not feature_open_for(user):
        raise TransitionError("FEATURE_DISABLED", "Secure eSIM setup isn't available yet. Please contact support.", 403)
    case.refresh_from_db()
    _require_esim_case(case)
    if case.esim_fulfilment != E.ACCESS_READY:
        raise TransitionError(
            "ACCESS_NOT_AVAILABLE",
            "Your eSIM setup details aren't available. If you've already opened them, please contact support.",
            409,
        )

    with transaction.atomic():
        grant = valid_access_grant(case)
        if grant is None:
            raise TransitionError("ACCESS_EXPIRED", "Your secure setup link has expired. Please contact support.", 412)
        grant = check_step_up(case, grant, StepUpGrant.Purpose.ESIM_INSTALL_ACCESS, timezone.now())
        try:
            profile = supplier.esim_profile(case)
        except supplier.ProfileUnavailable as exc:
            raise TransitionError(
                "SUPPLIER_UNAVAILABLE", "We can't fetch your eSIM details right now. Please try again shortly.", 503,
            ) from exc
        grant.consumed_at = timezone.now()
        grant.save(update_fields=["consumed_at"])
        # A system step on the customer's behalf (customers can only report installation).
        case = set_fulfilment(case, "esim", E.ACCESS_ISSUED, source=StatusSource.SYSTEM, actor=user)
        SimTypeChangeEvent.objects.create(
            tenant=case.tenant, case=case, actor=user, kind="esim_access_opened", status_source=StatusSource.CUSTOMER,
            version=case.version, detail={"grant": str(grant.uuid)},
        )
    return {
        "activation_code": profile.get("qr_code_value", ""),   # LPA:1$smdp$matching-id
        "smdp_address": profile.get("smdp_address", ""),
        "matching_id": profile.get("matching_id", ""),
        "qr_image": profile.get("qr_code_data_url", ""),       # data: URL, rendered in-session only
        "instructions": [
            "Connect your phone to Wi-Fi.",
            "Scan the QR code from another screen, or enter the details manually in your phone's eSIM settings.",
            "Do not share your activation code.",
            "Once installed, restart your phone and try a call and mobile data.",
        ],
        "one_time": True,
    }


def report_installed(user, case) -> SimTypeChangeCase:
    if case.esim_fulfilment != E.ACCESS_ISSUED:
        raise TransitionError("TRANSITION_NOT_ALLOWED", "There's nothing to confirm for this request.", 409)
    return set_fulfilment(case, "esim", E.INSTALLATION_REPORTED, source=StatusSource.CUSTOMER, actor=user)
