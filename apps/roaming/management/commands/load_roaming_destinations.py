"""Load or refresh the reference list of roaming destinations.

Usage:
    python manage.py load_roaming_destinations

Safe to run repeatedly: rows are matched on `code` and updated in place.
"""

import csv
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.roaming.models import Destination

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "destinations.csv"


class Command(BaseCommand):
    help = "Load the roaming destination reference list (ISO 3166-1 plus special networks)."

    def handle(self, *args, **options):
        created = updated = 0
        with DATA_FILE.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                _, was_created = Destination.objects.update_or_create(
                    code=row["code"].strip().upper(),
                    defaults={
                        "name": row["name"].strip(),
                        "kind": row["kind"].strip() or Destination.Kind.COUNTRY,
                        "parent_code": row["parent_code"].strip().upper(),
                        "aliases": row["aliases"].strip(),
                    },
                )
                created += was_created
                updated += not was_created
        self.stdout.write(self.style.SUCCESS(f"Destinations: {created} created, {updated} updated."))
