"""Email ICCID / IMSI / MSISDN for activated pSIMs that haven't been sent yet.

Physical SIMs don't need to wait on an async profile the way eSIMs do, so
this mostly exists to retry the cases the inline send-on-activation call
skipped (e.g. `billing_email` wasn't resolvable yet). Idempotent —
already-emailed SIMs are skipped — so it's safe to schedule alongside
`send_esim_qr`:

    python manage.py send_psim_details

Options:
    --limit N     process at most N SIMs this run (default 200)
    --iccid X     only this ICCID (manual retry / testing)
    --dry-run     report candidates without sending
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.sims.models import Sim
from apps.sims.psim import deliver_details


class Command(BaseCommand):
    help = "Email ICCID/IMSI/MSISDN details for activated pSIMs."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--iccid", type=str, default="")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        qs = Sim.objects.filter(
            inventory=Sim.Inventory.ACTIVATED,
            psim_details_emailed=False,
        ).exclude(
            Q(type_of_sim__icontains="euicc") | Q(type_of_sim__icontains="esim")
        )
        if opts["iccid"]:
            qs = qs.filter(iccid=opts["iccid"])
        qs = qs.order_by("activated_at")[: opts["limit"]]

        total = qs.count()
        if not total:
            self.stdout.write("No activated pSIMs awaiting a details email.")
            return

        if opts["dry_run"]:
            for sim in qs:
                self.stdout.write(f"[dry-run] would process pSIM {sim.iccid}")
            self.stdout.write(f"{total} pSIM(s) pending.")
            return

        sent = no_email = failed = 0
        for sim in qs:
            result = deliver_details(sim)
            if result == "sent":
                sent += 1
                self.stdout.write(self.style.SUCCESS(f"{sim.iccid}: details emailed"))
            elif result == "no_email":
                no_email += 1
                self.stdout.write(f"{sim.iccid}: activated, but no billing email yet")
            elif result.startswith("error"):
                failed += 1
                self.stderr.write(self.style.WARNING(f"{sim.iccid}: {result}"))
            # "already" / "not_psim" shouldn't occur given the filter; ignore.

        self.stdout.write(
            f"Done. sent={sent} no_email={no_email} failed={failed} of {total}."
        )
