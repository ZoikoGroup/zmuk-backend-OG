"""Read-only supplier lookups for SIM type change.

Reads are allowed regardless of `SIM_CHANGE_SUPPLIER_WRITES_ENABLED`; nothing
here changes anything at Transatel. A failed or unavailable read is reported
as "unknown", which callers treat as not actionable (CTO v2.0 §8: stale or
unknown supplier status is never actionable).
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass

from django.utils import timezone

from apps.sims.transatel import TransatelAPIError, TransatelError, get_service

from . import crypto

logger = logging.getLogger("apps.sim_change")

ACTIVE_STATUSES = {"active", "activated"}


@dataclass
class LineRead:
    ok: bool
    supplier_status: str = ""
    active: bool = False
    msisdn_matches: bool = False
    read_at: object = None
    correlation_id: str = ""


DETACHED_STATUSES = {"terminated", "deactivated", "cancelled", "canceled", "retired", "deleted"}
SUBSCRIBER_ID_KEYS = ("subscriberId", "subscriber_id", "subscriberID")


def _subscriber_id(data: dict) -> str:
    for key in SUBSCRIBER_ID_KEYS:
        if data.get(key):
            return str(data[key])
    return ""


def _read_serial(service, sim):
    """(data, detached_by_404) for one SIM; raises TransatelError on other failures."""
    try:
        return service.get_subscriber_by_serial(sim.serial_number or sim.iccid) or {}, False
    except TransatelAPIError as exc:
        if getattr(exc, "status_code", None) == 404:
            return {}, True
        raise


def read_binding(case):
    """Fresh supplier read proving (or not) that the swap moved the subscriber.

    Returns a saved `SupplierVerificationEvidence`, or None when the supplier
    can't be read (an unknown outcome — never treated as failed).

    Checks (Addendum v2.1 §4.2):
    * msisdn_matches / current_serial_is_target: the number from the
      before-snapshot is now on the reserved replacement SIM;
    * service_active: the replacement SIM is Active;
    * old_serial_detached: the old SIM is terminated, gone, or no longer
      carries the number;
    * subscriber_matches: the supplier subscriber ID is unchanged. If we hold
      no subscriber ID for the line, continuity of the number from the old SIM
      to the new one is used and recorded in `checks` (to confirm in UAT);
    * commercial_terms_unchanged: plan and term on the line equal the
      before-snapshot (allowances / add-ons / billing come from the supplier
      and usage platform and are recorded when available).
    """
    from ..models import SupplierVerificationEvidence

    if case.new_sim_id is None or case.old_sim_id is None:
        return None
    service = get_service()
    try:
        new_data, new_missing = _read_serial(service, case.new_sim)
        old_data, old_missing = _read_serial(service, case.old_sim)
    except TransatelError as exc:
        logger.warning("Binding read failed for case %s: %s", case.uuid, type(exc).__name__)
        return None

    snapshot = case.before_snapshot or {}
    expected_msisdn_fp = snapshot.get("msisdn_fingerprint", "")
    new_msisdn_fp = crypto.fingerprint(new_data.get("msisdn", ""))
    old_msisdn_fp = crypto.fingerprint(old_data.get("msisdn", ""))
    new_status = str(new_data.get("status", "") or "").strip()
    old_status = str(old_data.get("status", "") or "").strip().lower()

    number_on_new = bool(expected_msisdn_fp) and not new_missing and new_msisdn_fp == expected_msisdn_fp
    old_detached = old_missing or old_status in DETACHED_STATUSES or old_msisdn_fp != expected_msisdn_fp

    expected_sub = snapshot.get("supplier_subscriber_id", "") or ""
    new_sub = _subscriber_id(new_data)
    if expected_sub:
        subscriber_matches, basis = new_sub == expected_sub, "subscriber_id"
    else:
        subscriber_matches, basis = number_on_new and old_detached, "msisdn_continuity"

    line = case.line
    terms_unchanged = line.plan_id == snapshot.get("plan_id") and line.term == snapshot.get("term", "")

    digest = hashlib.sha256(json.dumps({"new": new_data, "old": old_data}, sort_keys=True, default=str).encode())
    return SupplierVerificationEvidence.objects.create(
        tenant=case.tenant, case=case, source="subscriber_read", subscriber_id=new_sub[:64],
        msisdn_matches=number_on_new, subscriber_matches=subscriber_matches, current_serial_is_target=number_on_new,
        old_serial_detached=old_detached, service_active=new_status.lower() in ACTIVE_STATUSES,
        commercial_terms_unchanged=terms_unchanged, supplier_status=new_status[:32],
        target_serial_fingerprint=crypto.fingerprint(case.new_sim.iccid),
        old_serial_fingerprint=crypto.fingerprint(case.old_sim.iccid),
        checks={"subscriber_basis": basis, "old_status": old_status[:32], "old_missing": old_missing,
                "new_missing": new_missing},
        response_sha256=digest.hexdigest(), fetched_at=timezone.now(),
    )


class ProfileUnavailable(Exception):
    """The eSIM profile couldn't be read from the supplier."""


def esim_profile(case) -> dict:
    """Live eSIM installation details for the case's replacement SIM.

    Fetched from Transatel at the moment of use and handed straight back:
    callers must never store, log or email the result (Addendum v2.1 A2).
    Raises ProfileUnavailable if the supplier can't be read or the profile
    isn't ready.
    """
    sim = case.new_sim
    if sim is None:
        raise ProfileUnavailable("no replacement SIM")
    try:
        profile = get_service().get_esim_qr(sim.serial_number or sim.iccid) or {}
    except TransatelError as exc:
        logger.warning("eSIM profile read failed for case %s: %s", case.uuid, type(exc).__name__)
        raise ProfileUnavailable("supplier unavailable") from exc
    if not profile.get("qr_code_value"):
        raise ProfileUnavailable("profile not ready")
    return profile


def read_line(line) -> LineRead:
    """Current supplier view of the line's SIM: status and whether the number agrees."""
    correlation_id = uuid.uuid4().hex[:16]
    sim = line.sim
    if sim is None:
        return LineRead(ok=False, correlation_id=correlation_id)
    try:
        data = get_service().get_subscriber_by_serial(sim.serial_number or sim.iccid) or {}
    except TransatelError as exc:
        logger.warning("Supplier read failed for line %s (ref %s): %s", line.uuid, correlation_id, type(exc).__name__)
        return LineRead(ok=False, correlation_id=correlation_id)

    status = str(data.get("status", "") or "").strip()
    supplier_msisdn = str(data.get("msisdn", "") or "")
    return LineRead(
        ok=True,
        supplier_status=status,
        active=status.lower() in ACTIVE_STATUSES,
        msisdn_matches=bool(line.current_msisdn) and crypto.fingerprint(supplier_msisdn) == crypto.fingerprint(
            line.current_msisdn
        ),
        read_at=timezone.now(),
        correlation_id=correlation_id,
    )
