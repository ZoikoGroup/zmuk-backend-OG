"""Keep number transfers moving when no webhook arrives.

Run periodically (cron / Celery beat), e.g. every 15 minutes:

    */15 * * * *  python manage.py reconcile_port_requests

* Sends transfers still waiting although their line is now active (e.g. the
  submission hit a Transatel outage).
* Lists in-flight transfers with no update for PORT_STALE_HOURS so support can
  chase them. Polling Transatel for their status needs the portability status
  endpoint, which is part of the open Transatel ticket.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.add_line.models import Line, PortRequest
from apps.add_line.services import porting


class Command(BaseCommand):
    help = "Submit waiting PAC/STAC requests and report stalled transfers."

    def handle(self, *args, **options):
        S = PortRequest.Status
        waiting = PortRequest.objects.filter(status=S.AWAITING_ACTIVATION, line__status=Line.Status.ACTIVE)
        submitted = 0
        for port in waiting:
            if porting.submit(port).status != S.AWAITING_ACTIVATION:
                submitted += 1

        cutoff = timezone.now() - timedelta(hours=settings.PORT_STALE_HOURS)
        stale = PortRequest.objects.filter(
            status__in=[S.PENDING, S.ATTACH_REQUESTED, S.ATTACH_RECEIVED, S.VALIDATED, S.LOCKED, S.CANCELING],
            updated_at__lte=cutoff,
        ).select_related("line")
        for port in stale:
            self.stdout.write(self.style.WARNING(
                f"Stalled: line {port.line.uuid} {port.code_type} {port.status} "
                f"since {port.updated_at:%Y-%m-%d %H:%M} (tx {port.supplier_transaction_id or '-'})"
            ))

        self.stdout.write(self.style.SUCCESS(
            f"Submitted {submitted} waiting transfer(s); {stale.count()} stalled."
        ))
