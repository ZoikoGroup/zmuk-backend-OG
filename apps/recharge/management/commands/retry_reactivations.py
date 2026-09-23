"""Retry failed SIM reactivations.

Run periodically (cron every 5 min, or celery beat):
    python manage.py retry_reactivations
"""

from django.core.management.base import BaseCommand
from apps.recharge.reactivation import retry_failed_reactivations


class Command(BaseCommand):
    help = "Retry failed/pending SIM reactivations"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50, help="Max attempts to retry")

    def handle(self, *args, **options):
        results = retry_failed_reactivations(limit=options["limit"])
        self.stdout.write(self.style.SUCCESS(
            f"Retries complete: {results['succeeded']} succeeded, "
            f"{results['failed']} failed, {results['skipped']} skipped."
        ))
