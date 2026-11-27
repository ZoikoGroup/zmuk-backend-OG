"""Keep SIM-change SIMs out of the legacy credential emails (CTO Addendum v2.1 A1).

For every SIM associated with a CT-005 case — the customer's current SIM or
the reserved replacement, in any case status — the purchase-journey jobs
(`send_esim_qr`, `send_psim_details`, inline delivery after activation) must
not fetch, store or email activation codes, QR payloads, IMSIs or ICCIDs.

Suppression is by case association, which is permanent: it does not depend
on a job flag that could be reset. SIM provenance (dedicated Swap SIM stock)
is added to this check when the Swap SIM inventory lands (v2.1 B4).

Existing purchase and Add-a-Number journeys are deliberately unchanged
(v2.1 A1); their remediation is a separate workstream.
"""

from django.db.models import Q


def change_case_sims_q() -> Q:
    """Filter on `Sim` matching every SIM linked to a SIM type change case."""
    from ..models import SimTypeChangeCase

    new_ids = SimTypeChangeCase.objects.filter(new_sim__isnull=False).values("new_sim_id")
    old_ids = SimTypeChangeCase.objects.filter(old_sim__isnull=False).values("old_sim_id")
    return Q(pk__in=new_ids) | Q(pk__in=old_ids)


def is_change_case_sim(sim) -> bool:
    from ..models import SimTypeChangeCase

    if sim is None or sim.pk is None:
        return False
    return SimTypeChangeCase.objects.filter(Q(new_sim_id=sim.pk) | Q(old_sim_id=sim.pk)).exists()
