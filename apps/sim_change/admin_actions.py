"""Network Ops / Security work-queue actions shown on a case in the admin.

Each action: who may do it (permission key in services.operations.PERM),
when it applies (`available`), the form it asks for, and the service call.
There is deliberately no action that confirms a swap or marks service ready:
only a supplier read can (CTO v2.0 §5, Addendum v2.1 acceptance test 5).
"""

from dataclasses import dataclass
from typing import Callable

from django import forms

from .models import PhysicalFulfilment, ProviderCommand, SimTypeChangeCase, StepUpGrant, SwapSimStock
from . import states
from .services import address, operations

S = SimTypeChangeCase.Status
P = PhysicalFulfilment


class VerificationForm(forms.Form):
    method = forms.ChoiceField(choices=StepUpGrant.Method.choices, initial=StepUpGrant.Method.SUPERVISED_SUPPORT)
    assurance = forms.ChoiceField(choices=StepUpGrant.Assurance.choices, label="Outcome")
    policy_version = forms.CharField(max_length=32, help_text="Version of the Security-approved procedure you followed.")
    external_reference = forms.CharField(max_length=128, required=False,
                                         help_text="Call or ticket reference. No passwords, codes or ID images.")
    risk_decision_id = forms.CharField(max_length=64, required=False)


class ConfirmForm(forms.Form):
    confirm = forms.BooleanField(label="I confirm")


class NoteForm(forms.Form):
    note = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), max_length=255)


class ReserveStockForm(forms.Form):
    supplier_check_reference = forms.CharField(
        max_length=128, help_text="Auriga check / ticket confirming this is an unassigned Swap SIM in our pool.",
    )
    stock = forms.ModelChoiceField(queryset=SwapSimStock.objects.none(), required=False,
                                   help_text="Leave empty to take the oldest eligible Swap SIM.")

    def __init__(self, *args, case=None, **kwargs):
        super().__init__(*args, **kwargs)
        if case is not None:
            self.fields["stock"].queryset = SwapSimStock.objects.filter(
                tenant=case.tenant, status=SwapSimStock.Status.AVAILABLE, sim_format=case.to_type,
            ).select_related("sim")


class DispatchForm(forms.Form):
    carrier = forms.CharField(max_length=64)
    tracking_reference = forms.CharField(max_length=64)


class ExecuteForm(forms.Form):
    target_iccid = forms.CharField(max_length=32, label="ICCID of the SIM swapped in Auriga")
    ticket_reference = forms.CharField(max_length=64)
    correlation_id = forms.CharField(max_length=128, required=False, label="Supplier correlation / transaction ID")


@dataclass
class Action:
    key: str
    label: str
    perm: str
    available: Callable[[SimTypeChangeCase], bool]
    form: type
    run: Callable  # (case, user, cleaned) -> str message
    shows_address: bool = False


def _pending_grant(case):
    return operations.pending_approvals().filter(case=case).first()


def _manual_command(case):
    command = operations.open_command(case)
    return command if command and command.channel == ProviderCommand.Channel.MANUAL_AURIGA else None


# ── when each action applies ──

def _can_verify(c):
    return c.status == S.VERIFY_REQUIRED and _pending_grant(c) is None


def _can_approve(c):
    return c.status == S.VERIFY_REQUIRED and _pending_grant(c) is not None


def _can_dispatch(c):
    return (c.to_type == "psim" and c.status == S.STOCK_RESERVED
            and c.physical_fulfilment in (P.NOT_STARTED, P.AWAITING_DISPATCH, P.EXCEPTION))


def _can_authorise(c):
    return c.status == S.STOCK_RESERVED and (c.to_type == "esim" or c.physical_fulfilment == P.RECEIPT_CONFIRMED)


def _can_execute(c):
    return c.status == S.PROVIDER_PENDING and _manual_command(c) is not None


def _can_release(c):
    return c.status in (S.HOLD_FRAUD, S.HOLD_CUSTOMER, S.SUPPORT_REQUIRED) and bool(c.held_from_status)


def _can_cancel(c):
    return S.CANCELLED in states.allowed_targets(c) and not c.supplier_submitted


# ── what each action does (returns the message shown to staff) ──

def _verify(case, user, d):
    grant = operations.record_verification(
        case, agent=user, method=d["method"], assurance=d["assurance"], policy_version=d["policy_version"],
        external_reference=d["external_reference"], risk_decision_id=d["risk_decision_id"],
    )
    if grant.assurance_result == StepUpGrant.Assurance.PASSED and grant.risk_tier == "high":
        return "Verification recorded. High-risk: a Security / Fraud colleague must approve it."
    return "Verification recorded."


def _approve(case, user, d):
    operations.approve_verification(_pending_grant(case), approver=user)
    return "Verification approved; the customer has been asked to confirm."


def _reserve(case, user, d):
    operations.reserve_stock(case, actor=user, supplier_check_reference=d["supplier_check_reference"],
                             stock_row=d["stock"])
    return "Replacement SIM reserved."


def _clear_review(case, user, d):
    operations.clear_address_review(case, reviewer=user, note=d["note"])
    return "Address review cleared."


def _dispatch(case, user, d):
    operations.record_dispatch(case, actor=user, carrier=d["carrier"], tracking_reference=d["tracking_reference"])
    return "Dispatch recorded."


def _delivered(case, user, d):
    operations.record_delivered(case, actor=user)
    return "Delivery recorded."


def _delivery_exception(case, user, d):
    operations.record_delivery_exception(case, actor=user, reason=d["note"])
    return "Delivery exception recorded."


def _authorise(case, user, d):
    operations.authorise(case, actor=user, reason=d["note"])
    return "Swap authorised for execution."


def _execute(case, user, d):
    operations.record_execution(_manual_command(case), actor=user, target_iccid=d["target_iccid"],
                                ticket_reference=d["ticket_reference"], correlation_id=d["correlation_id"])
    return "Execution recorded. The swap is confirmed only when the supplier read proves it."


def _release(case, user, d):
    operations.release_hold(case, actor=user, note=d["note"])
    return "Hold released."


def _cancel(case, user, d):
    operations.staff_cancel(case, actor=user, reason=d["note"])
    return "Request cancelled."


RECHECK_MESSAGES = {
    "confirmed": "The supplier confirms the new SIM; the line has been switched.",
    "pending": "The supplier doesn't show the swap yet. Check again later.",
    "unknown": "The supplier couldn't be read. Try again later.",
    "reconciliation": "The swap isn't visible in the expected time; the case is under reconciliation.",
    "split_brain": "The old SIM is detached but the new one isn't confirmed. Escalate to the supplier now.",
}


def _recheck(case, user, d):
    outcome = operations.recheck_supplier(case, actor=user)
    return RECHECK_MESSAGES.get(outcome, f"Supplier check: {outcome}.")


def _close_not_swapped(case, user, d):
    operations.close_not_swapped(case, actor=user, note=d["note"])
    return "Closed as failed: the supplier read shows the swap did not happen."


def _close(case, user, d):
    operations.close_completed(case, actor=user, note=d["note"])
    return "Request closed."


ACTIONS = [
    Action("verify", "Record identity verification", "verify", _can_verify, VerificationForm, _verify),
    Action("approve", "Approve high-risk verification", "approve", _can_approve, ConfirmForm, _approve),
    Action("reserve_stock", "Reserve replacement SIM", "stock", lambda c: c.status == S.CASE_ACCEPTED,
           ReserveStockForm, _reserve),
    Action("clear_address_review", "Clear delivery address review", "fraud",
           lambda c: c.is_open and address.REVIEW_FLAG in c.risk_flags, NoteForm, _clear_review, shows_address=True),
    Action("dispatch", "Record dispatch", "fulfil", _can_dispatch, DispatchForm, _dispatch, shows_address=True),
    Action("delivered", "Courier reports delivered", "fulfil",
           lambda c: c.is_open and c.physical_fulfilment == P.SHIPPED, ConfirmForm, _delivered),
    Action("delivery_exception", "Delivery problem (lost, returned...)", "fulfil",
           lambda c: c.is_open and c.physical_fulfilment in (P.AWAITING_DISPATCH, P.SHIPPED, P.DELIVERED),
           NoteForm, _delivery_exception),
    Action("authorise", "Authorise SIM swap", "authorise", _can_authorise, NoteForm, _authorise),
    Action("execute", "Record executed Auriga swap", "execute", _can_execute, ExecuteForm, _execute),
    Action("recheck", "Re-check with supplier now", "execute",
           lambda c: c.status in (S.SWAP_IN_PROGRESS, S.RECONCILIATION_REQUIRED), ConfirmForm, _recheck),
    Action("close_not_swapped", "Close: supplier shows no swap", "authorise",
           lambda c: c.status == S.RECONCILIATION_REQUIRED, NoteForm, _close_not_swapped),
    Action("close", "Close request (service confirmed)", "cases",
           lambda c: c.status == S.SERVICE_READY, NoteForm, _close),
    Action("release_hold", "Release hold", "cases", _can_release, NoteForm, _release),
    Action("cancel", "Cancel request", "cases", _can_cancel, NoteForm, _cancel),
]
BY_KEY = {a.key: a for a in ACTIONS}


def permitted(user, action: Action, case) -> bool:
    key = "fraud" if action.key == "release_hold" and case.status == S.HOLD_FRAUD else action.perm
    return bool(user.is_staff and user.has_perm(operations.PERM[key]))


def actions_for(user, case) -> list:
    return [a for a in ACTIONS if a.available(case) and permitted(user, a, case)]


def make_form(action: Action, case, data=None):
    if action.form is ReserveStockForm:
        return ReserveStockForm(data, case=case)
    return action.form(data)
