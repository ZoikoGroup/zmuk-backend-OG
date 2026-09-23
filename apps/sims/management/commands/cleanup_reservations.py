"""Release expired SIM cart holds.

Run periodically (cron / Celery beat), e.g. every 5 minutes:

    */5 * * * *  python manage.py cleanup_reservations

Mirrors the plugin's `transatel_cleanup_expired_reservations` cron.
"""

from django.core.management.base import BaseCommand

from apps.sims import reservations


class Command(BaseCommand):
    help = "Release SIM reservations whose hold has expired."

    def handle(self, *args, **options):
        result = reservations.release_expired()
        self.stdout.write(
            self.style.SUCCESS(
                f"Released {result['released']} expired reservation(s)."
            )
        )
