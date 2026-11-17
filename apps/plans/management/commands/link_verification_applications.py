"""Link public sector / student applications that have no account yet.

Applications sent before login was required are linked by email when the
migrations run. Run this again later to pick up customers who created an
account afterwards:

    python manage.py link_verification_applications
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.plans.eligibility import link_applications_by_email
from apps.public_sector.models import PublicSectorRegistration
from apps.student_discount.models import StudentDiscountApplication
from apps.student_discount.services import expire_lapsed


class Command(BaseCommand):
    help = "Link public sector and student applications to accounts by email."

    def handle(self, *args, **options):
        User = get_user_model()
        expire_lapsed()

        for label, model in (
            ("Public sector", PublicSectorRegistration),
            ("Student", StudentDiscountApplication),
        ):
            linked, skipped = link_applications_by_email(model, User, model.OPEN_STATUSES)
            self.stdout.write(f"{label}: linked {linked}, left unlinked {skipped}")
