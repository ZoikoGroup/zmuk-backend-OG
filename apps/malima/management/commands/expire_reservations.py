"""
Release reservations whose TTL has passed without a save-order callback.

Run from cron (or a periodic Celery beat job) every few minutes:

    */5 * * * * /path/to/venv/bin/python manage.py expire_reservations

Any active reservation with expires_at < now is marked `expired` and its
SIM is returned to the available pool. Safe to run repeatedly.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from malima.models import SimInventory, SimReservation


class Command(BaseCommand):
    help = "Release reservations whose TTL has passed."

    def handle(self, *args, **opts):
        now = timezone.now()
        released = 0
        with transaction.atomic():
            qs = (
                SimReservation.objects
                .select_for_update(skip_locked=True)
                .select_related("sim")
                .filter(status=SimReservation.Status.ACTIVE, expires_at__lt=now)
            )
            for r in qs:
                r.status = SimReservation.Status.EXPIRED
                r.released_at = now
                r.save(update_fields=["status", "released_at"])
                # Only put the SIM back if it's still in `reserved` — don't
                # touch it if ops have manually retired/suspended it.
                if r.sim_id and r.sim.status == SimInventory.Status.RESERVED:
                    r.sim.status = SimInventory.Status.AVAILABLE
                    r.sim.save(update_fields=["status", "updated_at"])
                released += 1

        if released:
            self.stdout.write(self.style.SUCCESS(
                f"Expired {released} stale reservation(s)."
            ))
        else:
            self.stdout.write("No stale reservations.")
