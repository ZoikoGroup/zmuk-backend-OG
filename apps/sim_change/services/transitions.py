"""The only way a SIM type change case changes status (v2.0 §6–§7, v2.1 A3/B1).

`transition()` moves the main lifecycle; `set_fulfilment()` moves the
physical-delivery or eSIM-installation track. Both:

1. lock the case row and re-read it (compare-and-set on `version` when the
   caller passes `expected_version`);
2. check the move against `states`;
3. apply the guards below;
4. bump `version`, set timestamps / action owner and append an event.

Main-lifecycle guards:
* customers can only accept, cancel or report fraud;
* VERIFIED needs a passed, unused StepUpGrant for this exact case, which is
  then consumed (v2.1 A3). High-risk grants also need a second approver;
* STOCK_RESERVED needs a reserved replacement SIM;
* PROVIDER_PENDING needs an authorised swap command (maker-checker, see
  `services.commands`) and, for a physical SIM, confirmed receipt;
* SWAP_IN_PROGRESS needs that command executed;
* SUPPLIER_BINDING_CONFIRMED needs fresh supplier evidence for this case where
  every invariant matched. Staff can't override supplier evidence;
* SERVICE_READY needs the eSIM setup path to be ready (eSIM target);
* CANCELLED is refused once the swap was executed at the supplier;
* FAILED after execution needs a supplier read showing the swap did not
  happen; otherwise it's RECONCILIATION_REQUIRED.
On a terminal status, unexecuted commands are cancelled and the line lock is
released.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .. import states
from ..models import (
    ActionOwner, EsimFulfilment, LineOperationLock, PhysicalFulfilment, ProviderCommand, SimTypeChangeCase,
    SimTypeChangeEvent, StatusSource, StepUpGrant, SupplierVerificationEvidence,
)
from . import line_lock

S = SimTypeChangeCase.Status
P = PhysicalFulfilment
E = EsimFulfilment

DEFAULT_ACTION_OWNER = {
    S.VERIFY_REQUIRED: ActionOwner.SUPPORT,
    S.AWAITING_CONFIRMATION: ActionOwner.CUSTOMER,
    S.CASE_ACCEPTED: ActionOwner.SUPPORT,
    S.STOCK_RESERVED: ActionOwner.NETWORK_OPS,
    S.PROVIDER_PENDING: ActionOwner.NETWORK_OPS,
    S.SWAP_IN_PROGRESS: ActionOwner.SUPPLIER,
    S.SERVICE_READY: ActionOwner.CUSTOMER,
    S.HOLD_FRAUD: ActionOwner.SECURITY,
    S.HOLD_CUSTOMER: ActionOwner.CUSTOMER,
    S.SUPPORT_REQUIRED: ActionOwner.SUPPORT,
    S.RECONCILIATION_REQUIRED: ActionOwner.NETWORK_OPS,
}

_STAMPS = {
    S.CASE_ACCEPTED: "approved_at",
    S.SWAP_IN_PROGRESS: "supplier_submitted_at",
    S.SUPPLIER_BINDING_CONFIRMED: "binding_confirmed_at",
    S.SERVICE_READY: "service_ready_at",
    S.CLOSED: "closed_at",
    S.FAILED: "closed_at",
    S.CANCELLED: "closed_at",
}

HIGH_RISK_TIERS = {"high", "elevated"}


class TransitionError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def evidence_max_age() -> timedelta:
    return timedelta(minutes=int(getattr(settings, "SIM_CHANGE_EVIDENCE_MAX_AGE_MINUTES", 15)))


def _lock_case(case, expected_version):
    case = SimTypeChangeCase.objects.select_for_update().get(pk=case.pk)
    if expected_version is not None and case.version != expected_version:
        raise TransitionError("CASE_CHANGED", "This request changed in the meantime. Refresh and try again.", 409)
    return case


def check_step_up(case: SimTypeChangeCase, grant, purpose: str, now) -> StepUpGrant:
    """Validate (and lock) a grant for `case` and `purpose`. Doesn't consume it."""
    if not isinstance(grant, StepUpGrant):
        raise TransitionError("STEP_UP_REQUIRED", "Identity verification is required.", 403)
    grant = StepUpGrant.objects.select_for_update().get(pk=grant.pk)
    if (grant.case_id != case.pk or grant.subject_id != case.owner_id or grant.line_id != case.line_id
            or grant.tenant != case.tenant):
        raise TransitionError("STEP_UP_MISMATCH", "This verification is for a different request.", 403)
    if grant.purpose != purpose:
        raise TransitionError("STEP_UP_MISMATCH", "This verification is for a different action.", 403)
    if grant.assurance_result != StepUpGrant.Assurance.PASSED:
        raise TransitionError("STEP_UP_NOT_PASSED", "Identity verification did not pass.", 403)
    if grant.revoked_at or grant.consumed_at:
        raise TransitionError("STEP_UP_USED", "This verification has already been used or withdrawn.", 403)
    if grant.expires_at <= now:
        raise TransitionError("STEP_UP_EXPIRED", "Identity verification has expired.", 412)
    if grant.risk_tier in HIGH_RISK_TIERS and (
        grant.approved_by_id is None or grant.approved_by_id == grant.verified_by_id
    ):
        raise TransitionError(
            "SECOND_APPROVAL_REQUIRED", "A high-risk verification needs approval by a second person.", 403,
        )
    return grant


def verifier_of(case: SimTypeChangeCase):
    """The staff member whose verification moved this case to VERIFIED."""
    grant = case.step_up_grants.filter(
        purpose=StepUpGrant.Purpose.SIM_TYPE_CHANGE, consumed_at__isnull=False,
    ).order_by("-consumed_at").first()
    return grant.verified_by_id if grant else None


def _check_evidence(case, evidence, now, *, must_confirm: bool) -> SupplierVerificationEvidence:
    if (not isinstance(evidence, SupplierVerificationEvidence) or evidence.case_id != case.pk
            or evidence.tenant != case.tenant):
        raise TransitionError("EVIDENCE_REQUIRED", "A supplier read for this case is required.", 422)
    if now - evidence.fetched_at > evidence_max_age():
        raise TransitionError("EVIDENCE_STALE", "The supplier read is too old; read the supplier again.", 412)
    if must_confirm and not evidence.binding_confirmed:
        raise TransitionError("BINDING_NOT_PROVEN", "The supplier read does not prove the new SIM is bound.", 422)
    return evidence


def _open_command(case):
    return case.commands.filter(status__in=ProviderCommand.OPEN_STATUSES).first()


def _guard_main(case, to_status, *, step_up, evidence, now):
    """Status-specific guards. Returns (grant_to_consume, evidence)."""
    grant = None
    if to_status == S.VERIFIED:
        grant = check_step_up(case, step_up, StepUpGrant.Purpose.SIM_TYPE_CHANGE, now)
    elif to_status == S.STOCK_RESERVED and case.new_sim_id is None:
        raise TransitionError("NO_STOCK_RESERVED", "Reserve a replacement SIM first.", 409)
    elif to_status == S.PROVIDER_PENDING:
        command = _open_command(case)
        if command is None or command.status != ProviderCommand.Status.PENDING or not command.authorised_by_id:
            raise TransitionError("SWAP_NOT_AUTHORISED", "The swap needs Network Ops authorisation first.", 409)
        if case.to_type == "psim" and case.physical_fulfilment != P.RECEIPT_CONFIRMED:
            raise TransitionError(
                "RECEIPT_NOT_CONFIRMED", "The customer must confirm they have the new SIM before the swap.", 409,
            )
    elif to_status == S.SWAP_IN_PROGRESS:
        command = _open_command(case)
        if command is None or command.status != ProviderCommand.Status.SENT:
            raise TransitionError("SWAP_NOT_EXECUTED", "The swap hasn't been executed at the supplier.", 409)
    elif to_status == S.SUPPLIER_BINDING_CONFIRMED:
        evidence = _check_evidence(case, evidence, now, must_confirm=True)
    elif to_status == S.SERVICE_READY and case.to_type == "esim" and case.esim_fulfilment not in states.ESIM_SETUP_READY:
        raise TransitionError("ESIM_SETUP_NOT_READY", "Secure eSIM setup isn't ready yet.", 409)
    elif to_status == S.CANCELLED and case.supplier_submitted:
        raise TransitionError(
            "CANCELLATION_UNAVAILABLE",
            "This change has already gone to our network partner, so it can't be cancelled here. "
            "Please contact support.",
            409,
        )
    elif to_status == S.FAILED and case.supplier_submitted:
        evidence = _check_evidence(case, evidence, now, must_confirm=False)
        if evidence.current_serial_is_target or evidence.old_serial_detached:
            raise TransitionError(
                "SWAP_OUTCOME_UNCLEAR",
                "The supplier read shows the swap may have happened; reconcile instead of failing.",
                409,
            )
    return grant, evidence


def transition(case: SimTypeChangeCase, to_status: str, *, source: str, actor=None, actor_role: str = "",
               reason: str = "", expected_version: int | None = None, step_up: StepUpGrant | None = None,
               evidence: SupplierVerificationEvidence | None = None, action_owner: str | None = None,
               detail: dict | None = None, trace_id: str = "") -> SimTypeChangeCase:
    if to_status not in S.values:
        raise TransitionError("UNKNOWN_STATUS", f"Unknown status {to_status!r}.", 422)
    if source not in StatusSource.values:
        raise TransitionError("UNKNOWN_SOURCE", f"Unknown status source {source!r}.", 422)

    with transaction.atomic():
        case = _lock_case(case, expected_version)
        before = case.status
        if to_status not in states.allowed_targets(case):
            raise TransitionError("TRANSITION_NOT_ALLOWED", f"Can't move from {before} to {to_status}.", 409)
        if source == StatusSource.CUSTOMER and to_status not in states.CUSTOMER_TARGETS:
            raise TransitionError("NOT_PERMITTED", "This step can't be taken by the customer.", 403)

        now = timezone.now()
        grant, evidence = _guard_main(case, to_status, step_up=step_up, evidence=evidence, now=now)

        # Holds remember where they came from; leaving a hold forgets it.
        if to_status in SimTypeChangeCase.HOLD_STATUSES:
            if before not in SimTypeChangeCase.HOLD_STATUSES:
                case.held_from_status = before
        else:
            case.held_from_status = ""

        case.status = to_status
        case.status_source = source
        case.version += 1
        case.action_owner = action_owner if action_owner is not None else DEFAULT_ACTION_OWNER.get(to_status, "")
        stamp = _STAMPS.get(to_status)
        if stamp and getattr(case, stamp) is None:
            setattr(case, stamp, now)
        case.save()

        if grant is not None:
            grant.consumed_at = now
            grant.save(update_fields=["consumed_at"])

        SimTypeChangeEvent.objects.create(
            tenant=case.tenant, case=case, actor=actor, actor_role=actor_role, kind="status_change",
            status_before=before, status_after=to_status, status_source=source, version=case.version,
            reason=reason[:255], evidence=evidence, detail=detail or {}, trace_id=trace_id,
        )

        if to_status in SimTypeChangeCase.TERMINAL_STATUSES:
            from . import stock  # local import: stock builds on this module

            case.commands.filter(status=ProviderCommand.Status.PENDING).update(
                status=ProviderCommand.Status.CANCELLED, updated_at=now,
            )
            stock.release_for_case(case, to_status.lower())
            line_lock.release(case.line, str(case.uuid), reason=f"case {to_status.lower()}")

    return case


def set_fulfilment(case: SimTypeChangeCase, track: str, to_status: str, *, source: str, actor=None,
                   actor_role: str = "", reason: str = "", expected_version: int | None = None,
                   carrier: str = "", tracking_reference: str = "", dispatch_source: str = "manual",
                   detail: dict | None = None) -> SimTypeChangeCase:
    """Move the physical-delivery (`track="physical"`) or eSIM (`"esim"`) track."""
    if track not in ("physical", "esim"):
        raise TransitionError("UNKNOWN_TRACK", f"Unknown fulfilment track {track!r}.", 422)
    if source not in StatusSource.values:
        raise TransitionError("UNKNOWN_SOURCE", f"Unknown status source {source!r}.", 422)
    field = "physical_fulfilment" if track == "physical" else "esim_fulfilment"

    with transaction.atomic():
        case = _lock_case(case, expected_version)
        before = getattr(case, field)
        if to_status not in states.allowed_fulfilment_targets(case, track):
            raise TransitionError(
                "TRANSITION_NOT_ALLOWED", f"Can't move {track} fulfilment from {before or 'not started'} "
                f"to {to_status}.", 409,
            )
        customer_targets = states.CUSTOMER_PHYSICAL_TARGETS if track == "physical" else states.CUSTOMER_ESIM_TARGETS
        if source == StatusSource.CUSTOMER and to_status not in customer_targets:
            raise TransitionError("NOT_PERMITTED", "This step can't be taken by the customer.", 403)
        if case.status == S.HOLD_FRAUD:
            raise TransitionError("CASE_ON_HOLD", "This request is on hold for a security review.", 409)

        now = timezone.now()
        if track == "physical" and to_status == P.AWAITING_DISPATCH and (
            case.status != S.STOCK_RESERVED or case.new_sim_id is None
        ):
            raise TransitionError("NO_STOCK_RESERVED", "Reserve a replacement SIM before dispatch.", 409)
        if track == "physical" and to_status == P.SHIPPED:
            if not carrier.strip() or not tracking_reference.strip():
                raise TransitionError("TRACKING_REQUIRED", "Record the carrier and tracking reference.", 422)
            case.carrier = carrier.strip()[:64]
            case.tracking_reference = tracking_reference.strip()[:64]
            case.dispatched_at = now
            case.dispatch_source = dispatch_source[:32]
            case.dispatched_by = actor
        if track == "esim" and to_status == E.PROFILE_AVAILABLE and case.status not in states.ESIM_ACCESS_AFTER:
            raise TransitionError(
                "BINDING_NOT_CONFIRMED", "eSIM setup opens only after the supplier confirms the swap.", 409,
            )

        setattr(case, field, to_status)
        case.version += 1
        case.save()
        SimTypeChangeEvent.objects.create(
            tenant=case.tenant, case=case, actor=actor, actor_role=actor_role, kind=f"{track}_fulfilment",
            status_before=before, status_after=to_status, status_source=source, version=case.version,
            reason=reason[:255], detail=detail or {},
        )
    return case


def open_case(*, owner, line, to_type: str, actor=None, consent: dict | None = None) -> SimTypeChangeCase:
    """Create a REQUESTED case and take the line's lock, in one transaction.

    Eligibility, verification and quote are later steps; this only records
    the request and its redacted before-snapshot.
    """
    from apps.add_line.models import SimType

    from ..models import Direction
    from .crypto import fingerprint

    with transaction.atomic():
        line = line_lock.lock_line_row(line.pk)
        if line.owner_id != owner.pk:
            raise TransitionError("LINE_NOT_FOUND", "We couldn't find that line on your account.", 404)
        if line.sim_id is None:
            raise TransitionError("NO_SIM_ON_LINE", "This line has no SIM to change.", 409)
        from_type = line.sim.sim_type_key
        if to_type not in SimType.values or to_type == from_type:
            raise TransitionError("INVALID_TARGET", "Choose the other SIM type for this line.", 422)

        case = SimTypeChangeCase(
            owner=owner, line=line, from_type=from_type, to_type=to_type,
            direction=Direction.for_types(from_type, to_type), old_sim=line.sim,
            status=S.REQUESTED, status_source=StatusSource.CUSTOMER, consent=consent or {},
            # The track that doesn't apply is NOT_REQUIRED from the start.
            physical_fulfilment=P.NOT_STARTED if to_type == SimType.PSIM else P.NOT_REQUIRED,
            esim_fulfilment=E.NOT_STARTED if to_type == SimType.ESIM else E.NOT_REQUIRED,
            before_snapshot={
                "line_status": line.status,
                "plan_id": line.plan_id,
                "term": line.term,
                "supplier_subscriber_id": line.supplier_subscriber_id,
                "msisdn_fingerprint": fingerprint(line.current_msisdn),
                "old_iccid_fingerprint": fingerprint(line.sim.iccid),
            },
        )
        try:
            line_lock.acquire(line, LineOperationLock.Operation.SIM_TYPE_CHANGE, str(case.uuid))
        except line_lock.LineBusy as exc:
            raise TransitionError(exc.code, str(exc), 409) from exc
        case.save()
        SimTypeChangeEvent.objects.create(
            tenant=case.tenant, case=case, actor=actor, kind="case_opened", status_after=S.REQUESTED,
            status_source=StatusSource.CUSTOMER, version=case.version,
        )
    return case
