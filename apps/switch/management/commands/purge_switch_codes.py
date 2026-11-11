"""Delete PAC/STAC codes held on switch requests once no longer needed (C13).

Run daily, next to purge_pac_codes:

    0 3 * * *  python manage.py purge_switch_codes

* Closed requests (completed, failed, cancelled): code removed
  PAC_RETENTION_DAYS after the last change.
* Any code older than its 30-day life + PAC_RETENTION_DAYS: it can't be used
  any more, so it is removed and the customer can send a new one.

Only the code goes; type, number and dates are kept.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.switch.models import SwitchRequest

CODE_LIFE_DAYS = 30


class Command(BaseCommand):
    help = "Remove PAC/STAC codes on switch requests past their retention period."

    def handle(self, *args, **options):
        now = timezone.now()
        keep = timedelta(days=settings.PAC_RETENTION_DAYS)
        purged = (
            SwitchRequest.objects.exclude(encrypted_code="")
            .filter(
                Q(status__in=SwitchRequest.CLOSED_STATUSES, updated_at__lte=now - keep)
                | Q(code_received_at__lte=now - timedelta(days=CODE_LIFE_DAYS) - keep)
            )
            # update() on purpose: no status change, so no history event.
            .update(encrypted_code="", code_purged_at=now)
        )
        self.stdout.write(self.style.SUCCESS(f"Purged {purged} switch PAC/STAC code(s)."))
