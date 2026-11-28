"""What the customer sees (CTO v2.0 §8 envelope, §14 approved wording).

Customer text comes from the CTO response §14 ("Approved wording for Product
review"). Statuses §14 doesn't cover use neutral text marked
`copy_source="engineering"` so Product can review it.

No secrets or full identifiers: the number is masked, the SIM shown by its
last four digits, the delivery address not returned at all.
"""

from datetime import timedelta

from django.utils import timezone

from .. import states
from ..models import EsimFulfilment, PhysicalFulfilment, SimTypeChangeCase
from . import eligibility

S = SimTypeChangeCase.Status

COPY = {
    "accepted": "We've received your request. Your current SIM remains in use unless we tell you otherwise. "
                "Track progress in MyZoiko.",
    "before_submit": "This is a request, not an instant SIM change. Any applicable charge will be shown before "
                     "you confirm.",
    "in_progress": "Your SIM change is being processed. You may temporarily lose mobile service. Please keep "
                   "another way to contact us.",
    "proven": "Your Zoiko Mobile service is ready on your new SIM. Follow the setup instructions to connect your "
              "device.",
    "esim_guidance": "Connect your phone to Wi-Fi. Open your secure eSIM setup details in MyZoiko. Do not share "
                     "your activation code.",
    "fraud": "We've paused this request to protect your number. Contact our security support team using the "
             "details shown here.",
    "ambiguous": "We're checking the status with our network partner. Please don't submit another request; we'll "
                 "update you as soon as we can.",
    "entry_esim": "Request a change to eSIM",
    "entry_psim": "Request a physical SIM",
    "entry_body": "We'll verify your details and guide you through the change. Your number and current plan will "
                  "stay the same.",
}
ENGINEERING_COPY = {
    "support": "We need to check something on your request before it can continue. Your current SIM remains in "
               "use unless we tell you otherwise.",
    "failed_before_swap": "We couldn't complete this request. Your current SIM is unchanged. Please contact "
                          "support if you'd like to try again.",
    "cancelled": "This request was cancelled. Your current SIM is unchanged.",
}

REPORT_WINDOW = timedelta(days=30)


def mask_msisdn(msisdn: str) -> str:
    digits = "".join(ch for ch in str(msisdn or "") if ch.isdigit())
    return f"•••• ••{digits[-4:]}" if len(digits) >= 4 else ""


def customer_message(case: SimTypeChangeCase) -> tuple[str, str]:
    """(text, copy_source) for the case's current status."""
    st = case.status
    if st == S.HOLD_FRAUD:
        return COPY["fraud"], "cto_s14"
    if st == S.RECONCILIATION_REQUIRED or (st in SimTypeChangeCase.HOLD_STATUSES and case.supplier_submitted):
        return COPY["ambiguous"], "cto_s14"
    if st in (S.SUPPORT_REQUIRED, S.HOLD_CUSTOMER):
        return ENGINEERING_COPY["support"], "engineering"
    if st == S.AWAITING_CONFIRMATION:
        return COPY["before_submit"], "cto_s14"
    if st == S.SWAP_IN_PROGRESS:
        return COPY["in_progress"], "cto_s14"
    if st in (S.SUPPLIER_BINDING_CONFIRMED, S.SERVICE_READY, S.CLOSED):
        if case.to_type == "esim" and case.esim_fulfilment in states.ESIM_SETUP_READY:
            return COPY["esim_guidance"], "cto_s14"
        return COPY["proven"], "cto_s14"
    if st == S.FAILED:
        if case.supplier_submitted:
            return "We couldn't confirm activation. Do not start another switch; check your request or contact support.", "ct005_s06"
        return ENGINEERING_COPY["failed_before_swap"], "engineering"
    if st == S.CANCELLED:
        return ENGINEERING_COPY["cancelled"], "engineering"
    return COPY["accepted"], "cto_s14"


def allowed_actions(case: SimTypeChangeCase) -> list:
    actions = []
    if S.CANCELLED in states.allowed_targets(case) and not case.supplier_submitted:
        actions.append("cancel")
    if case.status == S.AWAITING_CONFIRMATION:
        actions.append("confirm")
    if case.to_type == "psim" and case.physical_fulfilment in (PhysicalFulfilment.SHIPPED, PhysicalFulfilment.DELIVERED):
        actions.append("confirm_receipt")
    if case.to_type == "esim":
        from . import esim_access  # local import: esim_access builds on the staff services

        if (case.esim_fulfilment == EsimFulfilment.ACCESS_READY and esim_access.feature_open_for(case.owner)
                and esim_access.valid_access_grant(case) is not None):
            actions.append("esim_setup")
        if case.esim_fulfilment == EsimFulfilment.ACCESS_ISSUED:
            actions.append("report_installed")
    ended_recently = case.closed_at is None or timezone.now() - case.closed_at <= REPORT_WINDOW
    if case.status != S.HOLD_FRAUD and ended_recently:
        actions.append("report_unauthorised")
    return actions


def case_view(case: SimTypeChangeCase) -> dict:
    text, source = customer_message(case)
    shipped = case.to_type == "psim" and case.dispatched_at is not None
    return {
        "case_id": str(case.uuid),
        "line_id": str(case.line.uuid),
        "msisdn_masked": mask_msisdn(case.line.current_msisdn),
        "direction": case.direction,
        "from_type": case.from_type,
        "to_type": case.to_type,
        "status": case.status,
        "status_label": case.get_status_display(),
        "status_source": case.status_source,
        "action_owner": case.action_owner or None,
        "message": text,
        "copy_source": source,
        "allowed_actions": allowed_actions(case),
        "physical_fulfilment": (case.physical_fulfilment or None) if case.to_type == "psim" else None,
        "esim_fulfilment": (case.esim_fulfilment or None) if case.to_type == "esim" else None,
        "delivery": {
            "carrier": case.carrier, "tracking_reference": case.tracking_reference,
            "dispatched_at": case.dispatched_at,
        } if shipped else None,
        "new_sim_last4": case.new_sim.iccid[-4:] if case.new_sim_id and case.to_type == "psim" and shipped else None,
        "charge": (case.quote or {}).get("charge"),
        # The re-issued quote the customer confirms (only while it's waiting for them).
        "quote": {
            "quote_hash": case.quote_hash,
            "expires_at": (case.quote or {}).get("expires_at"),
            "requirements": (case.quote or {}).get("requirements", []),
        } if case.status == S.AWAITING_CONFIRMATION else None,
        "supplier_confirmed_at": case.binding_confirmed_at,
        "service_ready_at": case.service_ready_at,
        "created_at": case.created_at,
        "updated_at": case.updated_at,
        "version": case.version,
    }


def options_view(result) -> dict:
    line = result.line
    target = result.target_type or None
    return {
        "line_id": str(line.uuid),
        "msisdn_masked": mask_msisdn(line.current_msisdn),
        "plan": {"id": line.plan_id, "name": line.plan.name} if line.plan_id else None,
        "current_type": result.current_type or None,
        "line_status": line.status,
        "mode": eligibility.mode(),
        "available": result.eligible,
        "target": {
            "type": target,
            "label": COPY["entry_esim"] if target == "esim" else COPY["entry_psim"],
            "message": COPY["entry_body"],
        } if result.eligible and target else None,
        "blocked_reasons": [{"code": c, "message": eligibility.MESSAGES.get(c, "")} for c in result.blocked],
        "open_case_id": str(result.open_case.uuid) if result.open_case else None,
        "replacement_stock_available": result.stock_available,
        "supplier_read_at": result.read.read_at if result.read and result.read.ok else None,
        "correlation_id": result.read.correlation_id if result.read else None,
    }
