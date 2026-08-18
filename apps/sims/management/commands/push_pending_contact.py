"""Push deferred /contact-info to Transatel once activation has completed.

Transatel activation is asynchronous: /activate returns a transactionId and the
SIM stays AVAILABLE for a while, and /contact-info is rejected until the SIM is
Active. So during checkout we activate, stash the customer's contact block on
``Sim.pending_contact``, and return. This command drains that backlog:

    for each SIM with pending_contact set:
        look up its current subscriber status by serial;
        if it's no longer AVAILABLE -> push /contact-info, clear pending_contact;
        if still AVAILABLE          -> leave it for the next run.

Run it on a schedule (cron / Cloud Scheduler / Task) a few times over the first
several minutes after orders, e.g. every minute:

    python manage.py push_pending_contact

Options:
    --limit N     process at most N SIMs this run (default 200)
    --iccid X     only this ICCID (handy for manual retries / testing)
    --dry-run     report what would happen without calling Transatel
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.sims.models import Sim
from apps.sims.transatel import TransatelError, get_service
from apps.sims.transatel.exceptions import TransatelNotConfigured


class Command(BaseCommand):
    help = "Push deferred subscriber contact-info once SIMs are no longer AVAILABLE."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--iccid", type=str, default="")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        qs = Sim.objects.filter(pending_contact__isnull=False)
        if opts["iccid"]:
            qs = qs.filter(iccid=opts["iccid"])
        qs = qs.order_by("activated_at")[: opts["limit"]]

        total = qs.count()
        if not total:
            self.stdout.write("No SIMs with pending contact-info.")
            return

        if opts["dry_run"]:
            for sim in qs:
                self.stdout.write(f"[dry-run] would process {sim.iccid}")
            self.stdout.write(f"{total} SIM(s) pending.")
            return

        try:
            service = get_service()
        except TransatelNotConfigured as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        pushed = deferred = failed = 0
        for sim in qs:
            serial = sim.serial_number or sim.iccid

            # Is the SIM out of AVAILABLE yet?
            status = ""
            try:
                current = service.get_subscriber_by_serial(serial)
                status = str((current or {}).get("status", "") or "").strip()
            except TransatelError as exc:
                self.stderr.write(f"{sim.iccid}: status lookup failed — {exc}")
                failed += 1
                continue

            if not status or status.lower() == "available":
                deferred += 1
                self.stdout.write(f"{sim.iccid}: still {status or 'AVAILABLE'} — leaving for next run")
                continue

            # Active (or otherwise provisioned) — push the stored contact block.
            try:
                service.update_subscriber_contact(serial, sim.pending_contact or {})
            except TransatelError as exc:
                self.stderr.write(self.style.WARNING(f"{sim.iccid}: contact-info failed — {exc}"))
                failed += 1
                continue

            sim.pending_contact = None
            sim.save(update_fields=["pending_contact"])
            pushed += 1
            self.stdout.write(self.style.SUCCESS(f"{sim.iccid}: contact-info pushed (status {status})"))

        self.stdout.write(
            f"Done. pushed={pushed} deferred={deferred} failed={failed} of {total}."
        )
