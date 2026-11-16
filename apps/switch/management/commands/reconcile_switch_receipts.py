"""Daily check that every paid Switch & Save order has its payment record
and receipt (CTO document 03 §06, PR-35).

    30 3 * * *  python manage.py reconcile_switch_receipts

* Records missing captures (asks Stripe) and issues missing receipts.
* Receipts that keep failing stop after MAX_RECEIPT_ATTEMPTS and are
  listed in admin (Switch payment captures → receipt status "failed").
* A payment that doesn't match its order is never given a receipt; it's
  noted on the request for ops.
"""

from django.core.management.base import BaseCommand

from apps.switch import receipts


class Command(BaseCommand):
    help = "Record missing Switch & Save payments and issue missing receipts; report the rest."

    def handle(self, *args, **options):
        report = receipts.reconcile()
        self.stdout.write(
            f"Captures recorded: {report['captures_recorded']}; receipts issued: {report['receipts_issued']}; "
            f"still missing: {report['still_missing']}; needing ops (retries used up): {report['gave_up']}."
        )
        if report["still_missing"] or report["gave_up"]:
            self.stderr.write(self.style.WARNING("Some payments still have no receipt. Check the admin."))
