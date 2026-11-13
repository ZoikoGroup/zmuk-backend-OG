"""Daily PAC/STAC housekeeping for switch requests.

    0 3 * * *  python manage.py purge_switch_codes

Two separate jobs (engineering note, 13 Nov 2026):

1. Code validity (always on). A PAC/STAC is valid for 30 days. An open request
   whose code is past that moves to "action needed (code expired)", so the
   customer is asked for a new one. The code itself is kept.

2. Code deletion (off until approved). Removing the encrypted code from closed
   requests, or from codes past their life + PAC_RETENTION_DAYS. It only runs
   when SWITCH_CODE_DELETION_ENABLED is True, which waits for Operations and
   Legal to approve the retention rules. Until then the command only reports
   how many codes it would delete.

Neither job expires a request or deletes a request or any other personal data.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.switch import services
from apps.switch.models import SwitchRequest

S = SwitchRequest.Status
R = SwitchRequest.ActionReason


class Command(BaseCommand):
    help = "Flag expired PAC/STAC codes on open switch requests; delete codes only if approved."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without changing anything.",
        )

    def handle(self, *args, dry_run=False, **options):
        now = timezone.now()
        flagged = self._flag_expired_codes(now, dry_run)
        deleted, would_delete = self._delete_codes(now, dry_run)

        prefix = "[dry run] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}{flagged} open request(s) moved to action needed (code expired)."
        ))
        if deleted:
            self.stdout.write(self.style.SUCCESS(f"{prefix}Deleted {deleted} switch PAC/STAC code(s)."))
        if would_delete:
            self.stdout.write(self.style.WARNING(
                f"{would_delete} code(s) are due for deletion, but deletion is off "
                f"(SWITCH_CODE_DELETION_ENABLED) until the retention rules are approved."
            ))

    def _flag_expired_codes(self, now, dry_run):
        due = (
            SwitchRequest.objects.exclude(encrypted_code="")
            .filter(code_received_at__lte=services.code_expiry_cutoff(now))
            .exclude(status__in=SwitchRequest.CLOSED_STATUSES)
            .exclude(status=S.ACTION_NEEDED, action_reason=R.CODE_EXPIRED)
        )
        flagged = 0
        for switch in due:
            if not dry_run:
                switch.set_status(S.ACTION_NEEDED, reason=R.CODE_EXPIRED)
            flagged += 1
        return flagged

    def _delete_codes(self, now, dry_run):
        """(deleted, due_but_not_enabled)."""
        keep = timedelta(days=settings.PAC_RETENTION_DAYS)
        due = SwitchRequest.objects.exclude(encrypted_code="").filter(
            Q(status__in=SwitchRequest.CLOSED_STATUSES, updated_at__lte=now - keep)
            | Q(code_received_at__lte=services.code_expiry_cutoff(now) - keep)
        )
        if not getattr(settings, "SWITCH_CODE_DELETION_ENABLED", False):
            return 0, due.count()
        if dry_run:
            return due.count(), 0

        deleted = 0
        for switch in due:
            # Only the code goes; type, number, dates and the request stay.
            switch.encrypted_code = ""
            switch.code_purged_at = now
            switch.save(update_fields=["encrypted_code", "code_purged_at", "updated_at"])
            deleted += 1
        return deleted, 0
