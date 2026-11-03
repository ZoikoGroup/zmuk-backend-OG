"""Attaching an existing SIM to a customer account (decision A1).

A SIM is never linked because an email address matches. The customer gives
at least two identifiers; the claim is auto-approved only when:

    * the ICCID (printed on the SIM — proof of possession) and the order
      reference both point to the same activated SIM,
    * any mobile number given matches that SIM too,
    * the order was placed with this account's verified email, and
    * the SIM isn't already behind someone's live line.

Everything else goes to the support queue (`LineClaim` SUBMITTED). The
customer gets the same answer whether or not anything matched, so the
endpoint can't be used to probe which SIMs or numbers exist.
"""

import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.plans.models import Plan
from apps.sims.models import Order as SimOrder
from apps.sims.models import Sim

from ..models import Line, LineClaim
from . import audit
from .pac import PacError, normalise_msisdn

logger = logging.getLogger("apps.add_line")

MAX_OPEN_CLAIMS = 5


class ClaimError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _find_sim(iccid: str, msisdn: str):
    if iccid:
        return Sim.objects.filter(iccid=iccid).first()
    if msisdn:
        return Sim.objects.filter(msisdn=msisdn).first()
    return None


def _auto_approvable(user, claim: LineClaim, sim, order) -> bool:
    if not (settings.ADD_LINE_CLAIM_AUTO_APPROVE and claim.iccid and claim.order_reference):
        return False
    if not (sim and order and sim.order_reference == order.order_reference):
        return False
    if claim.msisdn and claim.msisdn != sim.msisdn:
        return False
    if sim.inventory != Sim.Inventory.ACTIVATED:
        return False
    email = (user.email or "").strip().lower()
    if not (user.is_active and email and email in {order.email.lower(), order.billing_email.lower()}):
        return False
    return not Line.objects.filter(sim=sim, status__in=Line.CAPACITY_STATUSES).exists()


def submit_claim(user, *, iccid="", msisdn="", order_reference="") -> LineClaim:
    iccid = (iccid or "").strip()
    order_reference = (order_reference or "").strip()
    if msisdn:
        try:
            msisdn = normalise_msisdn(msisdn)
        except PacError as exc:
            raise ClaimError(exc.code, exc.message)

    if sum(bool(v) for v in (iccid, msisdn, order_reference)) < 2:
        raise ClaimError("MORE_DETAILS_NEEDED",
                         "Give at least two of: the SIM's ICCID, its mobile number, your order reference.")
    if LineClaim.objects.filter(owner=user, status=LineClaim.Status.SUBMITTED).count() >= MAX_OPEN_CLAIMS:
        raise ClaimError("TOO_MANY_OPEN_CLAIMS",
                         "You already have claims being reviewed. We'll be in touch about those first.", 429)

    sim = _find_sim(iccid, msisdn)
    order = SimOrder.objects.filter(order_reference=order_reference).first() if order_reference else None

    claim = LineClaim.objects.create(
        owner=user, iccid=iccid, msisdn=msisdn, order_reference=order_reference,
        matched_sim=sim, matched_order=order,
        evidence={
            "sim_found": bool(sim),
            "order_found": bool(order),
            "sim_matches_order": bool(sim and order and sim.order_reference == order.order_reference),
            "msisdn_matches_sim": bool(sim and msisdn and sim.msisdn == msisdn),
        },
    )
    audit.record("claim_submitted", actor=user, after=claim.status, claim_id=claim.pk)

    if _auto_approvable(user, claim, sim, order):
        approve(claim, reviewer=None, note="auto: ICCID + order reference + verified account email")
        claim.refresh_from_db()
    return claim


def _line_status_for(sim: Sim) -> str:
    status = (sim.provisioning_status or "").strip().lower()
    if status == "suspended":
        return Line.Status.SUSPENDED
    if sim.inventory == Sim.Inventory.ACTIVATED or status in ("active", "activated"):
        return Line.Status.ACTIVE
    return Line.Status.PENDING_ACTIVATION


def _plan_for(order) -> Plan | None:
    """Best effort: the plan from the original checkout payload, if exactly one."""
    items = ((order.payload or {}).get("items") or []) if order else []
    codes = {str(i.get("transatelID")) for i in items if isinstance(i, dict) and i.get("transatelID")}
    if len(codes) != 1:
        return None
    return Plan.objects.filter(transatelID=codes.pop()).first()


def approve(claim: LineClaim, *, reviewer, note="") -> Line:
    with transaction.atomic():
        claim = LineClaim.objects.select_for_update().get(pk=claim.pk)
        if claim.status != LineClaim.Status.SUBMITTED:
            raise ClaimError("CLAIM_CLOSED", "This claim has already been decided.")
        sim = claim.matched_sim
        if not sim:
            raise ClaimError("NO_SIM", "Set the matched SIM before approving.")
        if Line.objects.filter(sim=sim, status__in=Line.CAPACITY_STATUSES).exists():
            raise ClaimError("SIM_ALREADY_LINKED", "This SIM is already on a live line.")

        line = Line.objects.create(
            owner=claim.owner, sim=sim, plan=_plan_for(claim.matched_order),
            status=_line_status_for(sim), source=Line.Source.LEGACY_CLAIM,
            current_msisdn=sim.msisdn or "", activated_at=sim.activated_at or sim.first_activation_date,
        )
        claim.status = LineClaim.Status.APPROVED
        claim.resulting_line = line
        claim.reviewed_by = reviewer
        claim.reviewed_at = timezone.now()
        claim.decision_note = note
        claim.save(update_fields=["status", "resulting_line", "reviewed_by", "reviewed_at", "decision_note"])
        audit.record("claim_approved", line=line, actor=reviewer, before=LineClaim.Status.SUBMITTED,
                     after=claim.status, claim_id=claim.pk, note=note)
    return line


def reject(claim: LineClaim, *, reviewer, note=""):
    updated = LineClaim.objects.filter(pk=claim.pk, status=LineClaim.Status.SUBMITTED).update(
        status=LineClaim.Status.REJECTED, reviewed_by=reviewer, reviewed_at=timezone.now(), decision_note=note,
    )
    if updated:
        audit.record("claim_rejected", actor=reviewer, before=LineClaim.Status.SUBMITTED,
                     after=LineClaim.Status.REJECTED, claim_id=claim.pk, note=note)
    return bool(updated)
