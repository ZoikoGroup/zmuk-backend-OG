"""Seed test SIM data for local recharge testing.

    python manage.py seed_test_sims

Creates 3 test SIMs so you can test phone validation without a real SIM park export.
"""

from django.core.management.base import BaseCommand
from apps.sims.models import Sim


TEST_SIMS = [
    {
        "iccid": "89441226663591001",
        "serial_number": "89441226663591001",
        "msisdn": "+447421118918",
        "imsi": "234201234567001",
        "type_of_sim": "UICC",
        "provisioning_status": "Suspended",
        "prepaid_status": "Suspended",
        "rate_plan": "MVNA Wholesale PAYM 7",
        "group": "ZOIKO eSIM",
        "inventory": "INSTOCK",
    },
    {
        "iccid": "89441226663591002",
        "serial_number": "89441226663591002",
        "msisdn": "+447421118919",
        "imsi": "234201234567002",
        "type_of_sim": "UICC",
        "provisioning_status": "Active",
        "prepaid_status": "Active",
        "rate_plan": "MVNA Wholesale PAYM 7",
        "group": "ZOIKO eSIM",
        "inventory": "OUTOFSTOCK",
    },
    {
        "iccid": "89441226663591003",
        "serial_number": "89441226663591003",
        "msisdn": "+447421118920",
        "imsi": "234201234567003",
        "type_of_sim": "eUICC",
        "provisioning_status": "Terminated",
        "prepaid_status": "Terminated",
        "rate_plan": "MVNA Wholesale PAYM 7",
        "group": "ZOIKO eSIM",
        "inventory": "OUTOFSTOCK",
    },
]


class Command(BaseCommand):
    help = "Seed test SIM data for local recharge testing"

    def handle(self, *args, **options):
        created = 0
        for sim_data in TEST_SIMS:
            obj, was_created = Sim.objects.update_or_create(
                iccid=sim_data["iccid"],
                defaults=sim_data,
            )
            if was_created:
                created += 1
                self.stdout.write(f"  Created: {sim_data['msisdn']} ({sim_data['provisioning_status']})")
            else:
                self.stdout.write(f"  Updated: {sim_data['msisdn']} ({sim_data['provisioning_status']})")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Test numbers:\n"
            f"  +447421118918  Suspended (rechargeable)\n"
            f"  +447421118919  Active (not rechargeable)\n"
            f"  +447421118920  Terminated (not rechargeable)\n"
        ))
