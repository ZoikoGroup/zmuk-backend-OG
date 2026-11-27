"""Allowed moves for a SIM type change case (v2.0 §6 as amended by v2.1 B1).

One primary lifecycle for both directions:

    REQUESTED → ELIGIBILITY_PENDING → VERIFY_REQUIRED → VERIFIED → QUOTED
    → AWAITING_CONFIRMATION → CASE_ACCEPTED → STOCK_RESERVED → PROVIDER_PENDING
    → SWAP_IN_PROGRESS → SUPPLIER_BINDING_CONFIRMED → SERVICE_READY → CLOSED

Physical delivery and eSIM installation are separate tracks
(`physical_fulfilment`, `esim_fulfilment`). The two meet the main lifecycle
only through guards in `services.transitions`:

* physical SIM: the swap (PROVIDER_PENDING) waits until the customer has
  confirmed receipt — courier "delivered" is not possession (v2.1 B5);
* eSIM: install access opens only after SUPPLIER_BINDING_CONFIRMED (UK
  Transatel/Auriga sequence, v2.1 A2). The order is fixed, not configurable;
* SERVICE_READY needs the applicable setup path to be ready. It is not proof
  that the handset connected.

Exceptions:
* HOLD_FRAUD, SUPPORT_REQUIRED and RECONCILIATION_REQUIRED from any open
  status; holds resume to the status they were entered from.
* HOLD_CUSTOMER only before anything goes to the supplier.
* CANCELLED only before the swap is executed (`supplier_submitted_at`).
* FAILED never straight from SWAP_IN_PROGRESS: an unclear result is
  RECONCILIATION_REQUIRED first.
"""

from .models import EsimFulfilment, PhysicalFulfilment, SimTypeChangeCase

S = SimTypeChangeCase.Status
P = PhysicalFulfilment
E = EsimFulfilment

MAIN_FLOW = {
    S.REQUESTED: {S.ELIGIBILITY_PENDING},
    S.ELIGIBILITY_PENDING: {S.VERIFY_REQUIRED},
    S.VERIFY_REQUIRED: {S.VERIFIED},
    S.VERIFIED: {S.QUOTED},
    S.QUOTED: {S.AWAITING_CONFIRMATION},
    S.AWAITING_CONFIRMATION: {S.CASE_ACCEPTED},
    S.CASE_ACCEPTED: {S.STOCK_RESERVED},
    S.STOCK_RESERVED: {S.PROVIDER_PENDING},
    S.PROVIDER_PENDING: {S.SWAP_IN_PROGRESS},
    S.SWAP_IN_PROGRESS: {S.SUPPLIER_BINDING_CONFIRMED},
    S.SUPPLIER_BINDING_CONFIRMED: {S.SERVICE_READY},
    S.SERVICE_READY: {S.CLOSED},
}

# Any open status may go to these; they resume to `held_from_status`.
ANY_OPEN_TO = {S.HOLD_FRAUD, S.SUPPORT_REQUIRED, S.RECONCILIATION_REQUIRED}

# Statuses where the swap has not been executed at the supplier yet.
PRE_SUBMISSION = {
    S.REQUESTED, S.ELIGIBILITY_PENDING, S.VERIFY_REQUIRED, S.VERIFIED, S.QUOTED,
    S.AWAITING_CONFIRMATION, S.CASE_ACCEPTED, S.STOCK_RESERVED, S.PROVIDER_PENDING,
}
CUSTOMER_HOLDABLE = PRE_SUBMISSION - {S.PROVIDER_PENDING}
FAILABLE = {S.ELIGIBILITY_PENDING, S.VERIFY_REQUIRED, S.STOCK_RESERVED} | set(SimTypeChangeCase.HOLD_STATUSES)

# What a customer may do on the main lifecycle; everything else is system, staff or supplier.
CUSTOMER_TARGETS = {S.CASE_ACCEPTED, S.CANCELLED, S.HOLD_FRAUD}

# ── Fulfilment tracks ──

PHYSICAL_FLOW = {
    P.NOT_STARTED: {P.AWAITING_DISPATCH},
    P.AWAITING_DISPATCH: {P.SHIPPED},
    P.SHIPPED: {P.DELIVERED, P.RECEIPT_CONFIRMED},  # customer may confirm before the courier updates
    P.DELIVERED: {P.RECEIPT_CONFIRMED},
    P.EXCEPTION: {P.AWAITING_DISPATCH},  # re-dispatch after a lost parcel etc.
}
ESIM_FLOW = {
    E.NOT_STARTED: {E.PROFILE_AVAILABLE},
    E.PROFILE_AVAILABLE: {E.ACCESS_READY},
    E.ACCESS_READY: {E.ACCESS_ISSUED},
    E.ACCESS_ISSUED: {E.INSTALLATION_REPORTED, E.INSTALLATION_VERIFIED},
    E.INSTALLATION_REPORTED: {E.INSTALLATION_VERIFIED},
    # Recovery needs a controlled supplier operation; it restarts at the profile.
    E.EXCEPTION: {E.PROFILE_AVAILABLE},
}
CUSTOMER_PHYSICAL_TARGETS = {P.RECEIPT_CONFIRMED}
CUSTOMER_ESIM_TARGETS = {E.INSTALLATION_REPORTED}

# eSIM setup paths that count as "ready" for SERVICE_READY.
ESIM_SETUP_READY = {E.ACCESS_READY, E.ACCESS_ISSUED, E.INSTALLATION_REPORTED, E.INSTALLATION_VERIFIED}
# Main statuses after which eSIM installation access may open (UK sequence).
ESIM_ACCESS_AFTER = {S.SUPPLIER_BINDING_CONFIRMED, S.SERVICE_READY}


def allowed_targets(case: SimTypeChangeCase) -> set:
    """Every main status this case may move to next, before guards are applied."""
    current = case.status
    if current in SimTypeChangeCase.TERMINAL_STATUSES:
        return set()

    targets = set(MAIN_FLOW.get(current, set()))
    targets |= ANY_OPEN_TO - {current}
    if current in SimTypeChangeCase.HOLD_STATUSES:
        if case.held_from_status:
            targets.add(case.held_from_status)
        if current == S.RECONCILIATION_REQUIRED:
            targets.add(S.SUPPLIER_BINDING_CONFIRMED)  # a reconciliation read may prove the swap happened
    if current in CUSTOMER_HOLDABLE:
        targets.add(S.HOLD_CUSTOMER)
    if current in PRE_SUBMISSION or current in SimTypeChangeCase.HOLD_STATUSES:
        targets.add(S.CANCELLED)  # still subject to the supplier_submitted_at guard
    if current in FAILABLE:
        targets.add(S.FAILED)
    return targets


def allowed_fulfilment_targets(case: SimTypeChangeCase, track: str) -> set:
    """Next statuses on the physical or eSIM track for this case's target type."""
    if not case.is_open:
        return set()
    if track == "physical" and case.to_type == "psim":
        current, flow, exception = case.physical_fulfilment, PHYSICAL_FLOW, P.EXCEPTION
    elif track == "esim" and case.to_type == "esim":
        current, flow, exception = case.esim_fulfilment, ESIM_FLOW, E.EXCEPTION
    else:
        return set()
    targets = set(flow.get(current, set()))
    if current not in (exception, P.NOT_STARTED):
        targets.add(exception)
    return targets
