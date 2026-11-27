"""Staff actions for the Phase 1 assisted journey (CTO Addendum v2.1 A3, B5).

Each action checks the staff member's permission, then delegates to the
transition / stock / command services, which enforce the rules. Roles
(see `services.roles`):

* Customer Security Support Agent: record identity verification.
* Security / Fraud: approve high-risk verifications, clear address reviews,
  release fraud holds.
* Network Ops Authoriser: reserve Swap SIMs, authorise the swap, dispatch.
* Network Ops Executor: record the executed Auriga swap, dispatch.
* QA / Audit: read only.

Separation of duties: the verifier can't approve their own high-risk
verification, authorise or execute the swap. Nobody can mark a swap as
confirmed or service as ready: only a supplier read can (step 4).
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import (
    PhysicalFulfilment, ProviderCommand, SimTypeChangeCase, SimTypeChangeEvent, StatusSource, StepUpGrant,
    SwapSimStock,
)
from . import address, commands, requests, stock
from .eligibility import HIGH_RISK_FLAGS
from .transitions import TransitionError, set_fulfilment, transition

S = SimTypeChangeCase.Status
P = PhysicalFulfilment

PERM = {
    "verify": "sim_change.verify_customer",
    "approve": "sim_change.approve_verification",
    "authorise": "sim_change.authorise_swap",
    "execute": "sim_change.execute_swap",
    "fulfil": "sim_change.manage_fulfilment",
    "stock": "sim_change.manage_stock",
    "fraud": "sim_change.review_fraud",
    "cases": "sim_change.manage_cases",
}


def require(user, key: str):
    if user is None or not user.is_authenticated or not user.is_staff or not user.has_perm(PERM[key]):
        raise TransitionError("PERMISSION_DENIED", "You don't have permission for this step.", 403)


def case_risk_tier(case: SimTypeChangeCase) -> str:
    return "high" if HIGH_RISK_FLAGS & set(case.risk_flags) else "standard"


def step_up_minutes() -> int:
    return int(getattr(settings, "SIM_CHANGE_STEP_UP_MINUTES", 60))


# ── Verification ──────────────────────────────────────────────────────────

def record_verification(case: SimTypeChangeCase, *, agent, method: str, assurance: str, policy_version: str,
                        external_reference: str = "", risk_decision_id: str = "") -> StepUpGrant:
    """Customer Security Support Agent records the supervised check.

    Passed + standard risk → the case is verified and re-quoted at once.
    Passed + high risk → waits for a Security / Fraud approver.
    Referred → fraud hold. Failed → the request fails (customer can try again).
    """
    require(agent, "verify")
    if not policy_version.strip():
        raise TransitionError("POLICY_VERSION_REQUIRED", "Record which verification procedure version was used.", 422)
    if method not in StepUpGrant.Method.values or assurance not in StepUpGrant.Assurance.values:
        raise TransitionError("INVALID_VERIFICATION", "Choose a verification method and outcome.", 422)
    if case.status != S.VERIFY_REQUIRED:
        raise TransitionError("TRANSITION_NOT_ALLOWED", "This request isn't waiting for verification.", 409)

    with transaction.atomic():
        grant = StepUpGrant.objects.create(
            tenant=case.tenant, case=case, line=case.line, subject=case.owner,
            purpose=StepUpGrant.Purpose.SIM_TYPE_CHANGE, verification_method=method, assurance_result=assurance,
            risk_tier=case_risk_tier(case), risk_decision_id=risk_decision_id.strip()[:64],
            policy_version=policy_version.strip()[:32], external_reference=external_reference.strip()[:128],
            verified_by=agent, expires_at=timezone.now() + timedelta(minutes=step_up_minutes()),
        )
        SimTypeChangeEvent.objects.create(
            tenant=case.tenant, case=case, actor=agent, actor_role="security_support_agent",
            kind="verification_recorded", status_source=StatusSource.SUPPORT, version=case.version,
            detail={"grant": str(grant.uuid), "assurance": assurance, "risk_tier": grant.risk_tier},
        )
        if assurance == StepUpGrant.Assurance.REFERRED:
            transition(case, S.HOLD_FRAUD, source=StatusSource.SUPPORT, actor=agent,
                       actor_role="security_support_agent", reason="verification referred to Security / Fraud")
        elif assurance == StepUpGrant.Assurance.FAILED:
            transition(case, S.FAILED, source=StatusSource.SUPPORT, actor=agent,
                       actor_role="security_support_agent", reason="identity verification failed")
        elif grant.risk_tier != "high":
            _continue_verified(case, grant, agent, "security_support_agent")
    return grant


def approve_verification(grant: StepUpGrant, *, approver) -> SimTypeChangeCase:
    """Security / Fraud second approval for a high-risk verification."""
    require(approver, "approve")
    with transaction.atomic():
        grant = StepUpGrant.objects.select_for_update().get(pk=grant.pk)
        if grant.verified_by_id == approver.pk:
            raise TransitionError("SEPARATION_OF_DUTIES", "You can't approve a verification you recorded.", 403)
        if grant.approved_by_id:
            raise TransitionError("ALREADY_APPROVED", "This verification has already been approved.", 409)
        grant.approved_by = approver
        grant.save(update_fields=["approved_by"])
        return _continue_verified(grant.case, grant, approver, "security_fraud")


def _continue_verified(case, grant, actor, role) -> SimTypeChangeCase:
    case = transition(case, S.VERIFIED, source=StatusSource.SUPPORT, actor=actor, actor_role=role, step_up=grant)
    return requests.requote_after_verification(case, actor=actor)


def pending_approvals():
    """High-risk passed verifications waiting for a Security / Fraud approver."""
    return StepUpGrant.objects.filter(
        purpose=StepUpGrant.Purpose.SIM_TYPE_CHANGE, assurance_result=StepUpGrant.Assurance.PASSED,
        risk_tier="high", approved_by__isnull=True, consumed_at__isnull=True, revoked_at__isnull=True,
        expires_at__gt=timezone.now(), case__status=S.VERIFY_REQUIRED,
    )


# ── Stock, dispatch, delivery ─────────────────────────────────────────────

def reserve_stock(case, *, actor, supplier_check_reference: str, stock_row: SwapSimStock | None = None):
    require(actor, "stock")
    return stock.reserve_for_case(case, actor=actor, supplier_check_reference=supplier_check_reference,
                                  stock=stock_row)


def quarantine_stock(stock_row: SwapSimStock, *, actor, reason: str):
    require(actor, "stock")
    return stock.quarantine(stock_row, reason=reason, actor=actor)


def clear_address_review(case, *, reviewer, note: str) -> SimTypeChangeCase:
    """Security / Fraud confirms the enhanced address review was done."""
    require(reviewer, "fraud")
    if not note.strip():
        raise TransitionError("NOTE_REQUIRED", "Record what was checked.", 422)
    with transaction.atomic():
        case = SimTypeChangeCase.objects.select_for_update().get(pk=case.pk)
        if address.REVIEW_FLAG not in case.risk_flags:
            raise TransitionError("NO_REVIEW_PENDING", "There's no address review on this request.", 409)
        case.risk_flags = [f for f in case.risk_flags if f != address.REVIEW_FLAG]
        case.version += 1
        case.save(update_fields=["risk_flags", "version", "updated_at"])
        SimTypeChangeEvent.objects.create(
            tenant=case.tenant, case=case, actor=reviewer, actor_role="security_fraud", kind="address_review_cleared",
            status_source=StatusSource.SUPPORT, version=case.version, reason=note.strip()[:255],
        )
    return case


def record_dispatch(case, *, actor, carrier: str, tracking_reference: str) -> SimTypeChangeCase:
    require(actor, "fulfil")
    if case.to_type != "psim":
        raise TransitionError("NOT_PHYSICAL", "Only a physical SIM is dispatched.", 409)
    if address.REVIEW_FLAG in case.risk_flags:
        raise TransitionError("ADDRESS_REVIEW_PENDING", "Security / Fraud must review the delivery address first.", 409)
    if not case.delivery_snapshot_encrypted:
        raise TransitionError("NO_DELIVERY_ADDRESS", "There's no validated delivery address on this request.", 409)
    with transaction.atomic():
        if case.physical_fulfilment in (P.NOT_STARTED, P.EXCEPTION):
            case = set_fulfilment(case, "physical", P.AWAITING_DISPATCH, source=StatusSource.SUPPORT, actor=actor,
                                  actor_role="network_ops")
        return set_fulfilment(case, "physical", P.SHIPPED, source=StatusSource.SUPPORT, actor=actor,
                              actor_role="network_ops", carrier=carrier, tracking_reference=tracking_reference)


def record_delivered(case, *, actor) -> SimTypeChangeCase:
    require(actor, "fulfil")
    return set_fulfilment(case, "physical", P.DELIVERED, source=StatusSource.SUPPORT, actor=actor,
                          actor_role="network_ops", reason="courier reports delivered")


def record_delivery_exception(case, *, actor, reason: str) -> SimTypeChangeCase:
    require(actor, "fulfil")
    if not reason.strip():
        raise TransitionError("REASON_REQUIRED", "Say what went wrong with the delivery.", 422)
    return set_fulfilment(case, "physical", P.EXCEPTION, source=StatusSource.SUPPORT, actor=actor,
                          actor_role="network_ops", reason=reason)


# ── The swap ──────────────────────────────────────────────────────────────

def authorise(case, *, actor, reason: str = "") -> ProviderCommand:
    require(actor, "authorise")
    if address.REVIEW_FLAG in case.risk_flags:
        raise TransitionError("ADDRESS_REVIEW_PENDING", "Security / Fraud must review the delivery address first.", 409)
    return commands.authorise_swap(case, authoriser=actor, reason=reason)


def record_execution(command: ProviderCommand, *, actor, target_iccid: str, ticket_reference: str,
                     correlation_id: str = "") -> ProviderCommand:
    require(actor, "execute")
    return commands.record_manual_execution(command, executor=actor, target_iccid=target_iccid,
                                            ticket_reference=ticket_reference, correlation_id=correlation_id)


# ── Holds and cancellation ────────────────────────────────────────────────

def release_hold(case, *, actor, note: str) -> SimTypeChangeCase:
    """Resume a held case to where it was. Fraud holds need Security / Fraud."""
    if case.status == S.HOLD_FRAUD:
        require(actor, "fraud")
    else:
        require(actor, "cases")
    if case.status not in SimTypeChangeCase.HOLD_STATUSES or not case.held_from_status:
        raise TransitionError("NOT_ON_HOLD", "This request isn't on hold.", 409)
    if case.status == S.RECONCILIATION_REQUIRED:
        raise TransitionError("RECONCILE_WITH_SUPPLIER", "Only a supplier read can resolve reconciliation.", 409)
    if not note.strip():
        raise TransitionError("NOTE_REQUIRED", "Record why the hold is being released.", 422)
    return transition(case, case.held_from_status, source=StatusSource.SUPPORT, actor=actor, reason=note)


def staff_cancel(case, *, actor, reason: str) -> SimTypeChangeCase:
    require(actor, "cases")
    if not reason.strip():
        raise TransitionError("REASON_REQUIRED", "Record why the request is being cancelled.", 422)
    return transition(case, S.CANCELLED, source=StatusSource.SUPPORT, actor=actor, reason=reason)


# ── Supplier checks and closing ───────────────────────────────────────────

def recheck_supplier(case, *, actor) -> str:
    """Network Ops asks for a fresh supplier read now (same rules as the job)."""
    require(actor, "execute")
    from . import reconcile

    return reconcile.check_case(case, reason=f"manual recheck by staff {actor.pk}")


def close_not_swapped(case, *, actor, note: str) -> SimTypeChangeCase:
    """Close a reconciliation case as failed, only on a fresh read showing no swap."""
    require(actor, "authorise")
    from . import reconcile

    return reconcile.fail_if_not_swapped(case, actor=actor, note=note)


def close_completed(case, *, actor, note: str) -> SimTypeChangeCase:
    """Support closes a case whose service is ready (v2.0 §5 step 7)."""
    require(actor, "cases")
    if not note.strip():
        raise TransitionError("NOTE_REQUIRED", "Record how the customer's service was confirmed.", 422)
    return transition(case, S.CLOSED, source=StatusSource.SUPPORT, actor=actor, reason=note)


def open_command(case) -> ProviderCommand | None:
    return case.commands.filter(status=ProviderCommand.Status.PENDING).first()
