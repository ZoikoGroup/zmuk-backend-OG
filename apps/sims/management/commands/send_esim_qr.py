"""Fetch and email the eSIM QR / LPA profile for activated eSIMs.

eSIM activation is asynchronous, so the QR profile (LPA string + PNG) isn't
ready the instant we activate. This command polls Transatel's esims endpoint for
each activated eSIM that hasn't had its QR emailed yet, stores the profile on the
SIM, and emails it to the customer (resolved from the order). It's idempotent —
already-emailed SIMs are skipped and a not-yet-ready profile is left for the
next run — so schedule it every minute or few for the first while after orders:

    python manage.py send_esim_qr

Options:
    --limit N     process at most N SIMs this run (default 200)
    --iccid X     only this ICCID (manual retry / testing)
    --dry-run     report candidates without calling Transatel
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.sims.esim import deliver_qr
from apps.sims.models import Sim
from apps.sims.transatel import get_service
from apps.sims.transatel.exceptions import TransatelNotConfigured


class Command(BaseCommand):
    help = "Fetch and email eSIM QR codes for activated eSIMs."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--iccid", type=str, default="")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        qs = Sim.objects.filter(
            inventory=Sim.Inventory.ACTIVATED,
            esim_qr_emailed=False,
        ).filter(
            Q(type_of_sim__icontains="euicc") | Q(type_of_sim__icontains="esim")
        )
        if opts["iccid"]:
            qs = qs.filter(iccid=opts["iccid"])
        qs = qs.order_by("activated_at")[: opts["limit"]]

        total = qs.count()
        if not total:
            self.stdout.write("No activated eSIMs awaiting a QR email.")
            return

        if opts["dry_run"]:
            for sim in qs:
                self.stdout.write(f"[dry-run] would process eSIM {sim.iccid}")
            self.stdout.write(f"{total} eSIM(s) pending.")
            return

        try:
            service = get_service()
        except TransatelNotConfigured as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        sent = not_ready = no_email = failed = 0
        for sim in qs:
            result = deliver_qr(service, sim)
            if result == "sent":
                sent += 1
                self.stdout.write(self.style.SUCCESS(f"{sim.iccid}: QR emailed"))
            elif result == "not_ready":
                not_ready += 1
                self.stdout.write(f"{sim.iccid}: profile not ready — retry next run")
            elif result == "no_email":
                no_email += 1
                self.stdout.write(f"{sim.iccid}: QR stored, but no customer email yet")
            elif result.startswith("error"):
                failed += 1
                self.stderr.write(self.style.WARNING(f"{sim.iccid}: {result}"))
            # "already" / "not_esim" shouldn't occur given the filter; ignore.

        self.stdout.write(
            f"Done. sent={sent} not_ready={not_ready} "
            f"no_email={no_email} failed={failed} of {total}."
        )
