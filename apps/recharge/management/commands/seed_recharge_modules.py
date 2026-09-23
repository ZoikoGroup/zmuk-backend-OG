# Reference seed logic. Turn into a management command:
#   apps/recharge/management/commands/seed_recharge_modules.py
from django.core.management.base import BaseCommand

from apps.recharge.models import RechargeModule

MODULES = [
    {"key": "recharge",     "name": "Recharge",     "category": "Recharge",     "shortcode": "[recharge]",     "sort_order": 1},
    {"key": "topup",        "name": "Top Up",       "category": "Top Up",       "shortcode": "[topup]",        "sort_order": 2},
    {"key": "pending_bill", "name": "Pending Bill", "category": "Pending Bill", "shortcode": "[pending_bill]", "sort_order": 3},
]


class Command(BaseCommand):
    help = "Seed the three recharge modules (Recharge / Top Up / Pending Bill), all Enabled."

    def handle(self, *args, **options):
        for m in MODULES:
            obj, created = RechargeModule.objects.update_or_create(
                key=m["key"],
                defaults={**m, "enabled": True},
            )
            self.stdout.write(("Created " if created else "Updated ") + obj.name)
        self.stdout.write(self.style.SUCCESS("Recharge modules seeded (all Enabled)."))
