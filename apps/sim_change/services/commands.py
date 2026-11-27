"""Supplier swap work items with maker-checker (v2.1 A3, v2.0 §9.2).

Phase 1 sequence for every supplier-executed change:

1. A Customer Security Support Agent verifies the customer (StepUpGrant).
2. `authorise_swap` — a Network Ops Authoriser, who is NOT the verifier,
   approves the work item. Creates one PENDING `ProviderCommand` and moves the
   case to PROVIDER_PENDING in the same transaction.
3. `record_manual_execution` — a Network Ops Executor, who is NOT the
   verifier, records that the Auriga SIM card swap was carried out on the
   reserved replacement SIM. Marks the command SENT and moves the case to
   SWAP_IN_PROGRESS. This is not success: only a later supplier read can
   confirm the binding.

Whether the executor may also be the authoriser is a setting
(`SIM_CHANGE_EXECUTOR_MUST_DIFFER_FROM_AUTHORISER`, default False); two
different people are always involved because neither may be the verifier.

Role permissions (who may authorise / execute at all) are checked by the
admin work queue; this module enforces the separation of duties.
"""

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import ProviderCommand, SimTypeChangeCase, SimTypeChangeEvent, StatusSource
from . import crypto
from .transitions import TransitionError, transition, verifier_of

S = SimTypeChangeCase.Status


def _require_staff(user, what):
    if user is None or not user.is_authenticated:
        raise TransitionError("STAFF_REQUIRED", f"A signed-in member of staff must {what}.", 403)


def authorise_swap(case: SimTypeChangeCase, *, authoriser, reason: str = "",
                   channel: str = ProviderCommand.Channel.MANUAL_AURIGA) -> ProviderCommand:
    _require_staff(authoriser, "authorise the swap")
    with transaction.atomic():
        case = SimTypeChangeCase.objects.select_for_update().get(pk=case.pk)
        if case.status != S.STOCK_RESERVED:
            raise TransitionError("TRANSITION_NOT_ALLOWED", "Only a case with a reserved SIM can be authorised.", 409)
        verifier = verifier_of(case)
        if verifier is None:
            raise TransitionError("NOT_VERIFIED", "The customer's identity hasn't been verified.", 409)
        if verifier == authoriser.pk:
            raise TransitionError(
                "SEPARATION_OF_DUTIES", "The person who verified the customer can't authorise the swap.", 403,
            )
        attempt = case.commands.count() + 1
        command = ProviderCommand.objects.create(
            tenant=case.tenant, case=case, channel=channel,
            idempotency_key=f"sim-swap-{case.uuid}-{attempt}",
            authorised_by=authoriser, authorised_at=timezone.now(),
        )
        transition(case, S.PROVIDER_PENDING, source=StatusSource.SUPPORT, actor=authoriser,
                   actor_role="network_ops_authoriser", reason=reason,
                   detail={"command": str(command.uuid)})
    return command


def record_manual_execution(command: ProviderCommand, *, executor, target_iccid: str,
                            ticket_reference: str, correlation_id: str = "") -> ProviderCommand:
    """Record that Network Ops executed the Auriga swap for this command."""
    _require_staff(executor, "record the swap")
    if not ticket_reference.strip():
        raise TransitionError("TICKET_REQUIRED", "Record the authorised ticket reference.", 422)
    with transaction.atomic():
        command = ProviderCommand.objects.select_for_update().get(pk=command.pk)
        case = SimTypeChangeCase.objects.select_for_update().get(pk=command.case_id)
        if command.channel != ProviderCommand.Channel.MANUAL_AURIGA or command.status != ProviderCommand.Status.PENDING:
            raise TransitionError("COMMAND_NOT_PENDING", "This work item isn't waiting for manual execution.", 409)
        if executor.pk == verifier_of(case):
            raise TransitionError(
                "SEPARATION_OF_DUTIES", "The person who verified the customer can't execute the swap.", 403,
            )
        if (getattr(settings, "SIM_CHANGE_EXECUTOR_MUST_DIFFER_FROM_AUTHORISER", False)
                and executor.pk == command.authorised_by_id):
            raise TransitionError(
                "SEPARATION_OF_DUTIES", "The person who authorised the swap can't also execute it.", 403,
            )
        # The swap must have targeted the SIM we reserved, nothing else.
        if case.new_sim_id is None or crypto.fingerprint(target_iccid) != crypto.fingerprint(case.new_sim.iccid):
            raise TransitionError("WRONG_TARGET_SIM", "That ICCID isn't the replacement SIM reserved for this case.", 422)

        now = timezone.now()
        command.status = ProviderCommand.Status.SENT
        command.executed_by = executor
        command.executed_at = now
        command.attempts += 1
        command.ticket_reference = ticket_reference.strip()[:64]
        command.correlation_id = correlation_id.strip()[:128]
        command.target_serial_masked = crypto.mask_serial(target_iccid)
        command.save()
        if command.correlation_id:
            case.supplier_correlation_id = command.correlation_id
            case.save(update_fields=["supplier_correlation_id", "updated_at"])
        SimTypeChangeEvent.objects.create(
            tenant=case.tenant, case=case, actor=executor, actor_role="network_ops_executor",
            kind="command_executed", status_source=StatusSource.SUPPORT, version=case.version,
            detail={"command": str(command.uuid), "target": command.target_serial_masked},
        )
        transition(case, S.SWAP_IN_PROGRESS, source=StatusSource.SUPPORT, actor=executor,
                   actor_role="network_ops_executor")
    return command
