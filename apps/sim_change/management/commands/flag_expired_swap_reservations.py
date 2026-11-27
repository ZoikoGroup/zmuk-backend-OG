"""Flag SIM type change cases whose Swap SIM reservation lapsed before dispatch or swap.

Moves each to SUPPORT_REQUIRED; the SIM stays reserved until a person decides.
Schedule hourly:

    python manage.py flag_expired_swap_reservations
"""

from django.core.management.base import BaseCommand

from apps.sim_change.services.stock import flag_expired_reservations


class Command(BaseCommand):
    help = "Send cases with an expired Swap SIM reservation to support."

    def handle(self, *args, **options):
        flagged = flag_expired_reservations()
        self.stdout.write(f"Flagged {flagged} case(s).")
