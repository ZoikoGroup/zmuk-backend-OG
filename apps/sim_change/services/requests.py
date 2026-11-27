"""Customer actions on a SIM type change (CTO v2.0 §8, Addendum v2.1).

`create_request` records a support-managed request: fresh eligibility and
risk check, the signed quote the customer accepted, their consent and (for a
physical SIM) the delivery address. It never touches the supplier. The case
ends up in VERIFY_REQUIRED, waiting for the Customer Security Support Agent.

After verification `requote_after_verification` moves VERIFIED → QUOTED →
AWAITING_CONFIRMATION, and the customer confirms with `confirm_request`.

`confirm_receipt` (physical SIM) needs the full ICCID printed on the new card,
compared without storing it, as proof of possession. Repeated wrong entries
send the case to support.

`report_unauthorised` opens a real security incident and pauses the case.
If the swap already happened, a database change can't undo it, so the
report is escalated for supplier containment instead (v2.1 §4.4).
"""

import logging
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.models import UserProfile
from apps.security.models import SuspiciousReport

from ..models import PhysicalFulfilment, SimTypeChangeCase, SimTypeChangeEvent, StatusSource
from . import address, crypto, eligibility, quote
from .transitions import TransitionError, open_case, set_fulfilment, transition

logger = logging.getLogger("apps.sim_change")
S = SimTypeChangeCase.Status
MAX_RECEIPT_ATTEMPTS = 5

BLOCK_STATUS = {"SUPPLIER_STATUS_UNAVAILABLE": 503, "FEATURE_DISABLED": 403}


def raise_if_blocked(result):
    if result.eligible:
        return
    code, message = eligibility.first_block(result)
    error = TransitionError(code, message, BLOCK_STATUS.get(code, 409))
    error.correlation_id = result.read.correlation_id if result.read else ""
    raise error


def check_consent(consent: dict):
    consent = consent or {}
    if consent.get("accepted") is not True:
        raise TransitionError("CONSENT_REQUIRED", "Please confirm you understand this is a request.", 422)
    if str(consent.get("wording_version") or "") != settings.SIM_CHANGE_CONSENT_VERSION:
        raise TransitionError("CONSENT_OUTDATED", "The request wording has changed. Please review it again.", 412)


def address_source_for(user, clean: dict) -> str:
    """'on_file' when the postcode matches an address already on the account, else 'new'.

    Accounts hold free-text addresses today, so the postcode is the reliable
    comparison. A new address always gets enhanced review.
    """
    profile = UserProfile.objects.filter(user=user).first()
    if profile is None:
        return SimTypeChangeCase.AddressSource.NEW
    postcode = clean["postcode"].replace(" ", "").upper()
    for text in (profile.delivery_address, profile.billing_address):
        if postcode and postcode in re.sub(r"\s", "", text or "").upper():
            return SimTypeChangeCase.AddressSource.ON_FILE
    return SimTypeChangeCase.AddressSource.NEW


def create_request(user, line, data: dict, *, meta: dict | None = None) -> SimTypeChangeCase:
    to_type = data.get("target_type")
    result = eligibility.assess(user, line, to_type=to_type)
    if to_type not in ("esim", "psim") or to_type == result.current_type:
        raise TransitionError("INVALID_TARGET", "Choose the other SIM type for this line.", 422)
    raise_if_blocked(result)
    if quote.device_problems(to_type, data.get("device")):
        raise TransitionError("DEVICE_NOT_READY", "Please confirm your device is ready for this change.", 422)
    accepted = quote.verify_quote(user, result, data.get("quote"))
    check_consent(data.get("consent"))
    clean_address = address.normalise_uk_address(data.get("delivery_address")) if to_type == "psim" else None

    meta = meta or {}
    consent = {
        "wording_version": settings.SIM_CHANGE_CONSENT_VERSION,
        "accepted_at": meta.get("now"),
        "ip_address": meta.get("ip"),
        "user_agent": (meta.get("user_agent") or "")[:255],
    }
    with transaction.atomic():
        case = open_case(owner=user, line=line, to_type=to_type, actor=user, consent=consent)
        case.quote = accepted
        case.quote_hash = accepted["quote_hash"]
        case.quote_version = accepted["quote_version"]
        case.risk_flags = result.risk_flags
        case.save(update_fields=["quote", "quote_hash", "quote_version", "risk_flags", "updated_at"])
        case = transition(case, S.ELIGIBILITY_PENDING, source=StatusSource.SYSTEM,
                          detail={"risk_tier": result.risk_tier, "history": result.history_status,
                                  "supplier_ref": result.read.correlation_id if result.read else ""})
        if clean_address is not None:
            case = address.set_delivery_address(
                case, clean_address, source=address_source_for(user, clean_address), actor=user,
            )
        case = transition(case, S.VERIFY_REQUIRED, source=StatusSource.SYSTEM)
    return case


def requote_after_verification(case: SimTypeChangeCase, *, actor=None) -> SimTypeChangeCase:
    """VERIFIED → QUOTED → AWAITING_CONFIRMATION with a fresh signed quote."""
    if case.status != S.VERIFIED:
        raise TransitionError("TRANSITION_NOT_ALLOWED", "The request hasn't been verified yet.", 409)
    result = eligibility.assess(case.owner, case.line, to_type=case.to_type, read_supplier=False)
    result.blocked = [b for b in result.blocked if b != "OPEN_REQUEST"]  # this case itself
    raise_if_blocked(result)
    # The customer confirmed their device when they made the request.
    checks = quote.ESIM_DEVICE_CHECKS if case.to_type == "esim" else quote.PSIM_DEVICE_CHECKS
    fresh = quote.build_quote(case.owner, result, device=dict.fromkeys(checks, True))
    with transaction.atomic():
        case = transition(case, S.QUOTED, source=StatusSource.SYSTEM, actor=actor)
        case.quote, case.quote_hash, case.quote_version = fresh, fresh["quote_hash"], fresh["quote_version"]
        case.save(update_fields=["quote", "quote_hash", "quote_version", "updated_at"])
        case = transition(case, S.AWAITING_CONFIRMATION, source=StatusSource.SYSTEM, actor=actor)
    return case


def confirm_request(user, case: SimTypeChangeCase, submitted_quote: dict) -> SimTypeChangeCase:
    if case.status != S.AWAITING_CONFIRMATION:
        raise TransitionError("TRANSITION_NOT_ALLOWED", "There's nothing to confirm on this request.", 409)
    stored = case.quote or {}
    if not crypto.same_text(stored.get("quote_hash", ""), str((submitted_quote or {}).get("quote_hash", ""))):
        raise TransitionError("QUOTE_CHANGED", "The details have changed. Please review the latest quote.", 422)
    expires = parse_datetime(stored.get("expires_at") or "")
    if expires is None or expires <= timezone.now():
        raise TransitionError("QUOTE_EXPIRED", "This quote has expired. Please contact support to refresh it.", 412)
    return transition(case, S.CASE_ACCEPTED, source=StatusSource.CUSTOMER, actor=user, expected_version=case.version)


def cancel_request(user, case: SimTypeChangeCase, expected_version: int | None = None) -> SimTypeChangeCase:
    return transition(case, S.CANCELLED, source=StatusSource.CUSTOMER, actor=user, expected_version=expected_version,
                      reason="cancelled by customer")


def confirm_receipt(user, case: SimTypeChangeCase, iccid: str) -> SimTypeChangeCase:
    if case.to_type != "psim" or case.physical_fulfilment not in (
        PhysicalFulfilment.SHIPPED, PhysicalFulfilment.DELIVERED,
    ):
        raise TransitionError("TRANSITION_NOT_ALLOWED", "There's no SIM on its way for this request.", 409)
    failures = case.events.filter(kind="receipt_check_failed").count()
    if failures >= MAX_RECEIPT_ATTEMPTS:
        raise TransitionError("RECEIPT_ATTEMPTS_EXCEEDED", "Please contact support to confirm your new SIM.", 429)

    if case.new_sim_id is None or not crypto.same_identifier(crypto.fingerprint(case.new_sim.iccid), iccid):
        with transaction.atomic():
            SimTypeChangeEvent.objects.create(
                tenant=case.tenant, case=case, actor=user, kind="receipt_check_failed",
                status_source=StatusSource.CUSTOMER, version=case.version,
            )
            if failures + 1 >= MAX_RECEIPT_ATTEMPTS:
                transition(case, S.SUPPORT_REQUIRED, source=StatusSource.SYSTEM,
                           reason="too many wrong SIM numbers entered on receipt")
        raise TransitionError("ICCID_MISMATCH", "That number doesn't match the SIM we sent. Check the number on the card.", 422)
    return set_fulfilment(case, "physical", PhysicalFulfilment.RECEIPT_CONFIRMED, source=StatusSource.CUSTOMER,
                          actor=user)


def report_unauthorised(user, case: SimTypeChangeCase, note: str = "") -> SimTypeChangeCase:
    """Open a security incident and pause the case wherever possible."""
    note = (note or "").strip()[:1000]
    with transaction.atomic():
        report = SuspiciousReport.objects.create(
            user=user, note=f"SIM type change {case.uuid} reported as not authorised by the customer. {note}".strip(),
        )
        SimTypeChangeEvent.objects.create(
            tenant=case.tenant, case=case, actor=user, kind="unauthorised_report",
            status_source=StatusSource.CUSTOMER, version=case.version,
            detail={"report_id": report.pk, "supplier_submitted": case.supplier_submitted, "status": case.status},
        )
        case = SimTypeChangeCase.objects.select_for_update().get(pk=case.pk)
        case.fraud_case_reference = case.fraud_case_reference or f"SR-{report.pk}"
        case.save(update_fields=["fraud_case_reference", "updated_at"])
        if case.is_open and case.status != S.HOLD_FRAUD:
            case = transition(case, S.HOLD_FRAUD, source=StatusSource.CUSTOMER, actor=user,
                              reason="customer reported the request as not authorised")
    # Supplier containment and Security/Ops alerting are human actions; make
    # sure it's loud in the logs until the notification channel exists.
    logger.critical("SIM type change %s reported unauthorised (report SR-%s, supplier_submitted=%s)",
                    case.uuid, report.pk, case.supplier_submitted)
    return case
