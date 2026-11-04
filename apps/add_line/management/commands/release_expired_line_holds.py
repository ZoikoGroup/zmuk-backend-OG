"""Close abandoned add-line checkouts and release their slot holds.

Run periodically (cron / Celery beat), e.g. every 5 minutes:

    */5 * * * *  python manage.py release_expired_line_holds

Unpaid orders past their hold have their Stripe PaymentIntent cancelled and
their SIM returned to stock. Paid holds (no expiry) are never touched.
"""

from django.core.management.base import BaseCommand

from apps.add_line.services import capacity, checkout


class Command(BaseCommand):
    help = "Close abandoned add-line checkouts and release expired capacity holds."

    def handle(self, *args, **options):
        closed = checkout.expire_abandoned()
        released = capacity.release_expired()
        self.stdout.write(self.style.SUCCESS(
            f"Closed {closed} abandoned checkout(s); released {released} expired line hold(s)."
        ))
