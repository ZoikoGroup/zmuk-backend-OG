"""Retry paid add-line orders that are not live yet.

Run periodically (cron / Celery beat), e.g. every 5 minutes:

    */5 * * * *  python manage.py retry_add_line_fulfilment

Covers activation failures, SIMs still provisioning, and orders paid while
the webhook was down. Each order gets at most fulfilment.MAX_ATTEMPTS failed
attempts; after that it stays FAILED for support to handle.
"""

from django.core.management.base import BaseCommand

from apps.add_line.models import AddLineOrder
from apps.add_line.services import fulfilment


class Command(BaseCommand):
    help = "Retry activation for paid add-line orders that aren't live yet."

    def handle(self, *args, **options):
        live = 0
        orders = fulfilment.pending_retry()
        for order in orders:
            order = fulfilment.fulfil(order)
            if order.fulfilment_status == AddLineOrder.FulfilmentStatus.ACTIVATED:
                live += 1
        self.stdout.write(self.style.SUCCESS(
            f"Checked {len(orders)} order(s); {live} now live."
        ))
