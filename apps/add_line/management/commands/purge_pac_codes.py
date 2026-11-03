"""Delete stored PAC/STAC codes that are no longer needed (decision C13).

Run daily:

    0 3 * * *  python manage.py purge_pac_codes

* Finished transfers (done, cancelled, failed): code removed PAC_RETENTION_DAYS
  after the last status change.
* Anything else older than the code's 30-day life + PAC_RETENTION_DAYS: the
  code can't be used any more, so it is removed too. A request still waiting
  for activation is then closed as ERROR so the customer can send a new code.

Audit metadata (type, number, statuses, dates) is kept; only the code goes.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.add_line.models import PortRequest
from apps.add_line.services import audit

CODE_LIFE_DAYS = 30


class Command(BaseCommand):
    help = "Remove PAC/STAC codes past their retention period."

    def handle(self, *args, **options):
        now = timezone.now()
        keep = timedelta(days=settings.PAC_RETENTION_DAYS)
        due = PortRequest.objects.exclude(encrypted_code="").filter(
            Q(status__in=PortRequest.TERMINAL_STATUSES, updated_at__lte=now - keep)
            | Q(created_at__lte=now - timedelta(days=CODE_LIFE_DAYS) - keep)
        )

        purged = 0
        for port in due:
            port.encrypted_code = ""
            port.code_purged_at = now
            fields = ["encrypted_code", "code_purged_at", "updated_at"]
            if port.status == PortRequest.Status.AWAITING_ACTIVATION:
                port.status = PortRequest.Status.ERROR
                port.last_error = {"detail": "switch code expired before the line was activated"}
                fields += ["status", "last_error"]
                audit.record("port_status", line=port.line, port_request=port,
                             before=PortRequest.Status.AWAITING_ACTIVATION, after=port.status,
                             source="purge")
            port.save(update_fields=fields)
            purged += 1

        self.stdout.write(self.style.SUCCESS(f"Purged {purged} PAC/STAC code(s)."))
