"""pSIM details delivery: email ICCID / IMSI / MSISDN to the customer.

Physical SIMs have no QR/LPA profile to install — the customer instead needs
the card's ICCID, IMSI, and MSISDN. Unlike the eSIM QR (which waits on an
async Transatel lookup), these values already live on the Sim row once
activation succeeds, so delivery is immediate. Idempotent — once a SIM's
details have been emailed it's skipped.
"""

from __future__ import annotations

import logging

from .contacts import customer_for
from .emails import send_psim_details_email

logger = logging.getLogger("apps.sims")


def deliver_details(sim) -> str:
    """Email the pSIM's ICCID / IMSI / MSISDN for one SIM.

    Returns one of: "sent", "already", "not_psim", "suppressed", "no_email",
    "error:<msg>". Safe to call repeatedly; only sends once.

    "suppressed": the SIM belongs to a SIM type change case (CT-005), whose
    ICCID / IMSI must never be emailed (Addendum v2.1 A1).
    """
    if sim.psim_details_emailed:
        return "already"
    if sim.is_esim:
        return "not_psim"
    from apps.sim_change.services.suppression import is_change_case_sim
    if is_change_case_sim(sim):
        return "suppressed"

    email, name = customer_for(sim)
    if not email:
        logger.info("pSIM %s activated but no customer email on file yet.", sim.iccid)
        return "no_email"

    sent = send_psim_details_email(email, name, sim, sim.order_reference)
    if sent:
        sim.psim_details_emailed = True
        sim.save(update_fields=["psim_details_emailed"])
        return "sent"
    return "error:email_send_failed"
