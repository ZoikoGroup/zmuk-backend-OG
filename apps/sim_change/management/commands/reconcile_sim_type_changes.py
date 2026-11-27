"""Read the supplier for every executed or unclear SIM type change and act on it.

Confirms and cuts over proven swaps, flags overdue or partial ones for
reconciliation. Read-only towards Transatel; never resends a swap.
Schedule every few minutes:

    python manage.py reconcile_sim_type_changes
"""

from django.core.management.base import BaseCommand

from apps.sim_change.services.reconcile import reconcile_open_cases


class Command(BaseCommand):
    help = "Reconcile SIM type change cases with the supplier."

    def handle(self, *args, **options):
        counts = reconcile_open_cases()
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to check"
        self.stdout.write(summary)
