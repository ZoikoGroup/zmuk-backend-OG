"""Seed the RechargeProduct table with plans matching the live WordPress site.

Plans taken directly from the 'Select Recharge Plan' modal screenshots.

Run once after migration (safe to re-run — uses update_or_create):
    python manage.py seed_recharge_products
"""

from django.core.management.base import BaseCommand
from apps.recharge.models import RechargeProduct


# Exact plans from the WordPress site screenshots
# rate_plan left blank = uses TRANSATEL["RATE_PLAN"] from settings (MVNA Wholesale PAYM 7)
PRODUCTS = [
    {
        "name": "Everyday 3GB+",
        "slug": "everyday-3gb-plus",
        "module": "recharge",
        "price_pence": 490,   # £4.90
        "short_description": "3GB+ data, unlimited calls & texts",
        "rate_plan": "",
        "attributes": {"data_gb": 3, "validity_days": 30},
        "sort_order": 1,
    },
    {
        "name": "10GB Data",
        "slug": "10gb-data",
        "module": "recharge",
        "price_pence": 1055,  # £10.55
        "short_description": "10GB data, unlimited calls & texts",
        "rate_plan": "",
        "attributes": {"data_gb": 10, "validity_days": 30},
        "sort_order": 2,
    },
    {
        "name": "Zoiko Unlimited Data",
        "slug": "zoiko-unlimited-data",
        "module": "recharge",
        "price_pence": 2950,  # £29.50
        "short_description": "Unlimited data, unlimited calls & texts",
        "rate_plan": "",
        "attributes": {"data_gb": 0, "unlimited": True, "validity_days": 30},
        "sort_order": 3,
    },
    {
        "name": "Zoiko Elite 100GB",
        "slug": "zoiko-elite-100gb",
        "module": "recharge",
        "price_pence": 2834,  # £28.34
        "short_description": "100GB data, unlimited calls & texts",
        "rate_plan": "",
        "attributes": {"data_gb": 100, "validity_days": 30},
        "sort_order": 4,
    },
    {
        "name": "Zoiko Max 30GB",
        "slug": "zoiko-max-30gb",
        "module": "recharge",
        "price_pence": 1699,  # £16.99
        "short_description": "30GB data, unlimited calls & texts",
        "rate_plan": "",
        "attributes": {"data_gb": 30, "validity_days": 30},
        "sort_order": 5,
    },
    {
        "name": "Zoiko Standard 10GB",
        "slug": "zoiko-standard-10gb",
        "module": "recharge",
        "price_pence": 1214,  # £12.14
        "short_description": "10GB data, unlimited calls & texts",
        "rate_plan": "",
        "attributes": {"data_gb": 10, "validity_days": 30},
        "sort_order": 6,
    },
    {
        "name": "Zoiko Plus 3GB",
        "slug": "zoiko-plus-3gb",
        "module": "recharge",
        "price_pence": 566,   # £5.66
        "short_description": "3GB data, unlimited calls & texts",
        "rate_plan": "",
        "attributes": {"data_gb": 3, "validity_days": 30},
        "sort_order": 7,
    },
    {
        "name": "Zoiko Connect 1GB",
        "slug": "zoiko-connect-1gb",
        "module": "recharge",
        "price_pence": 404,   # £4.04
        "short_description": "1GB data, unlimited calls & texts",
        "rate_plan": "",
        "attributes": {"data_gb": 1, "validity_days": 30},
        "sort_order": 8,
    },
]


class Command(BaseCommand):
    help = "Seed RechargeProduct table with plans matching the live WordPress site"

    def handle(self, *args, **options):
        created = 0
        updated = 0

        for p in PRODUCTS:
            obj, was_created = RechargeProduct.objects.update_or_create(
                slug=p["slug"],
                defaults=p,
            )
            if was_created:
                self.stdout.write(f"  Created: {obj.name} — {obj.price_display}")
                created += 1
            else:
                self.stdout.write(f"  Updated: {obj.name} — {obj.price_display}")
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nDone: {created} created, {updated} updated."
        ))
