from django.core.management.base import BaseCommand

from apps.sim_orders.models import SimProduct

PRODUCTS = [
    {"name": "Everyday 3GB+",  "slug": "everyday-3gb",  "plan_name": "Everyday 3GB+",  "price_pence": 500},
    {"name": "Everyday 5GB+",  "slug": "everyday-5gb",  "plan_name": "Everyday 5GB+",  "price_pence": 800},
    {"name": "Everyday 10GB+", "slug": "everyday-10gb", "plan_name": "Everyday 10GB+", "price_pence": 1200},
    {"name": "Unlimited Data", "slug": "unlimited-data","plan_name": "Unlimited Data", "price_pence": 2000},
]


class Command(BaseCommand):
    help = "Seed example SIM products so the buy flow can be tested."

    def handle(self, *args, **options):
        for p in PRODUCTS:
            obj, created = SimProduct.objects.update_or_create(
                slug=p["slug"], defaults={**p, "is_active": True}
            )
            self.stdout.write(("Created " if created else "Updated ") + obj.name)
        self.stdout.write(self.style.SUCCESS("SIM products seeded."))
