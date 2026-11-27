"""Supplier reconciliation and the local cutover (CTO v2.0 §9, Addendum v2.1 §4.2).

`check_case` reads the supplier for a case whose swap was executed
(SWAP_IN_PROGRESS) or is unclear (RECONCILIATION_REQUIRED) and decides:

* every invariant proven → SUPPLIER_BINDING_CONFIRMED and, in the same
  transaction, the local cutover:
    - `Line.sim` → the replacement SIM; the number moves to it (recharge and
      other lookups find SIMs by number) and is cleared from the old SIM;
    - old SIM → RETIRED; replacement SIM → ACTIVATED; Swap stock → ASSIGNED;
    - the supplier command → ACKNOWLEDGED.
  A physical SIM then goes straight to SERVICE_READY (the customer already
  has the card). An eSIM waits at SUPPLIER_BINDING_CONFIRMED for secure
  install access (step 5);
* old SIM detached but the new one not proven (split brain) → reconciliation
  at once, with a CRITICAL alert;
* nothing changed yet → wait, until `SIM_CHANGE_RECONCILE_AFTER_MINUTES`
  after execution, then reconciliation;
* supplier unreadable → treated as unknown, never as failed.

Nothing here retries or sends a swap. `fail_if_not_swapped` lets staff close a
reconciliation case only when a fresh read shows the swap did not happen.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.sims.models import Sim

from ..models import ProviderCommand, SimTypeChangeCase, StatusSource, SwapSimStock
from . import supplier
from .transitions import TransitionError, transition

logger = logging.getLogger("apps.sim_change")
S = SimTypeChangeCase.Status

CHECKABLE = (S.SWAP_IN_PROGRESS, S.RECONCILIATION_REQUIRED)


def reconcile_after() -> timedelta:
    # Placeholder until supplier UAT measures real swap timings (v2.0 §9.2).
    return timedelta(minutes=int(getattr(settings, "SIM_CHANGE_RECONCILE_AFTER_MINUTES", 30)))


def _overdue(case) -> bool:
    started = case.supplier_submitted_at or case.updated_at
    return timezone.now() - started > reconcile_after()


def _to_reconciliation(case, reason, **detail):
    if case.status == S.RECONCILIATION_REQUIRED:
        return case
    return transition(case, S.RECONCILIATION_REQUIRED, source=StatusSource.SUPPLIER_POLL, reason=reason, detail=detail)


def _cut_over(case, evidence) -> SimTypeChangeCase:
    """Confirm the binding and switch the line, atomically."""
    with transaction.atomic():
        case = transition(case, S.SUPPLIER_BINDING_CONFIRMED, source=StatusSource.SUPPLIER_POLL, evidence=evidence)
        line = case.line.__class__.objects.select_for_update().get(pk=case.line_id)
        old_sim = Sim.objects.select_for_update().get(pk=case.old_sim_id)
        new_sim = Sim.objects.select_for_update().get(pk=case.new_sim_id)
        number = line.current_msisdn or old_sim.msisdn

        old_sim.inventory = Sim.Inventory.RETIRED
        old_sim.msisdn = ""
        old_sim.save(update_fields=["inventory", "msisdn"])

        now = timezone.now()
        new_sim.inventory = Sim.Inventory.ACTIVATED
        new_sim.msisdn = number
        new_sim.activated_at = now
        new_sim.save(update_fields=["inventory", "msisdn", "activated_at"])

        line.sim = new_sim
        line.save(update_fields=["sim", "updated_at"])

        SwapSimStock.objects.filter(sim=new_sim).update(status=SwapSimStock.Status.ASSIGNED, updated_at=now)
        case.commands.filter(status__in=ProviderCommand.OPEN_STATUSES).update(
            status=ProviderCommand.Status.ACKNOWLEDGED, acknowledged_at=now, updated_at=now,
        )
        if case.to_type == "psim":
            case = transition(case, S.SERVICE_READY, source=StatusSource.SYSTEM,
                              reason="supplier confirmed the new SIM; customer already has it")
    logger.info("SIM type change %s confirmed by supplier read; line %s now on its new SIM", case.uuid, line.uuid)
    return case


def check_case(case: SimTypeChangeCase, *, reason: str = "scheduled") -> str:
    """Read the supplier for one case. Returns what happened."""
    case.refresh_from_db()
    if case.status not in CHECKABLE:
        return "not_applicable"

    evidence = supplier.read_binding(case)
    if evidence is None:
        if case.status == S.SWAP_IN_PROGRESS and _overdue(case):
            _to_reconciliation(case, "supplier could not be read after the swap", trigger=reason)
            return "reconciliation"
        return "unknown"

    if evidence.binding_confirmed:
        _cut_over(case, evidence)
        return "confirmed"

    if evidence.old_serial_detached and not (evidence.current_serial_is_target and evidence.service_active):
        _to_reconciliation(case, "old SIM no longer bound but the new SIM isn't confirmed", trigger=reason,
                           evidence=evidence.pk)
        logger.critical("SIM type change %s: possible partial cutover (evidence %s); customer may have no service",
                        case.uuid, evidence.pk)
        return "split_brain"

    if case.status == S.SWAP_IN_PROGRESS and _overdue(case):
        _to_reconciliation(case, "swap not visible at the supplier in the expected time", trigger=reason,
                           evidence=evidence.pk)
        return "reconciliation"
    return "pending"


def case_for_serial(serial) -> SimTypeChangeCase | None:
    """The executed / unclear case whose new or old SIM has this serial (webhook routing)."""
    serial = str(serial)
    return SimTypeChangeCase.objects.filter(status__in=CHECKABLE).filter(
        Q(new_sim__iccid=serial) | Q(new_sim__serial_number=serial)
        | Q(old_sim__iccid=serial) | Q(old_sim__serial_number=serial)
    ).first()


def reconcile_open_cases() -> dict:
    counts = {}
    for case in SimTypeChangeCase.objects.filter(status__in=CHECKABLE).order_by("updated_at"):
        try:
            outcome = check_case(case)
        except TransitionError as exc:  # the case moved on meanwhile
            outcome = f"skipped:{exc.code}"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def fail_if_not_swapped(case: SimTypeChangeCase, *, actor, note: str) -> SimTypeChangeCase:
    """Close a reconciliation case as FAILED, only on a fresh read showing no swap."""
    if case.status != S.RECONCILIATION_REQUIRED:
        raise TransitionError("TRANSITION_NOT_ALLOWED", "Only a case under reconciliation can be closed this way.", 409)
    if not note.strip():
        raise TransitionError("NOTE_REQUIRED", "Record what was checked with the supplier.", 422)
    evidence = supplier.read_binding(case)
    if evidence is None:
        raise TransitionError("SUPPLIER_UNAVAILABLE", "The supplier can't be read right now; try again later.", 503)
    with transaction.atomic():
        case = transition(case, S.FAILED, source=StatusSource.SUPPORT, actor=actor, reason=note, evidence=evidence)
        case.commands.filter(status__in=ProviderCommand.OPEN_STATUSES).update(
            status=ProviderCommand.Status.FAILED, updated_at=timezone.now(),
        )
    return case
