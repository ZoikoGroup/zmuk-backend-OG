"""
Import SIM inventory from CSV.

Usage:
    python manage.py import_sims path/to/sims.csv
    python manage.py import_sims path/to/sims.csv --batch Q2-2026-FR

Expected CSV columns (header row required):
    msisdn,imsi,iccid,sim_type,roaming_zone,batch,notes

`sim_type` defaults to "eSIM" if blank. `roaming_zone` and `batch` may be
blank. Existing rows (same msisdn/imsi/iccid) are skipped, not overwritten —
use the Django admin to edit individual records.
"""

from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from malima.models import SimInventory


class Command(BaseCommand):
    help = "Import SIM inventory from a CSV file."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str)
        parser.add_argument(
            "--batch", default="",
            help="Override the batch tag on every row.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Validate the file but don't write anything.",
        )

    def handle(self, *args, **opts):
        path = Path(opts["csv_path"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")

        override_batch = opts["batch"]
        dry_run = opts["dry_run"]

        created = skipped = invalid = 0
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            required = {"msisdn", "imsi", "iccid"}
            missing = required - set((reader.fieldnames or []))
            if missing:
                raise CommandError(f"CSV is missing columns: {', '.join(missing)}")

            existing = set(SimInventory.objects.values_list("msisdn", flat=True))

            new_rows: list[SimInventory] = []
            for row in reader:
                msisdn = (row.get("msisdn") or "").strip()
                imsi = (row.get("imsi") or "").strip()
                iccid = (row.get("iccid") or "").strip()
                if not (msisdn and imsi and iccid):
                    invalid += 1
                    continue
                if msisdn in existing:
                    skipped += 1
                    continue

                new_rows.append(SimInventory(
                    msisdn=msisdn,
                    imsi=imsi,
                    iccid=iccid,
                    sim_type=(row.get("sim_type") or "eSIM").strip() or "eSIM",
                    roaming_zone=(row.get("roaming_zone") or "").strip(),
                    batch=override_batch or (row.get("batch") or "").strip(),
                    notes=(row.get("notes") or "").strip(),
                ))
                existing.add(msisdn)
                created += 1

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"[dry-run] would create {created}, skip {skipped}, "
                f"reject {invalid} invalid row(s)."
            ))
            return

        with transaction.atomic():
            SimInventory.objects.bulk_create(new_rows, batch_size=500)

        self.stdout.write(self.style.SUCCESS(
            f"Imported {created} SIM(s). Skipped {skipped} existing, "
            f"rejected {invalid} invalid."
        ))
