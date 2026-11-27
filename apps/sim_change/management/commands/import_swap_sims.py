"""Import Swap SIM stock for Change SIM type (whole file or nothing).

Usage:
    python manage.py import_swap_sims swap_stock.csv --source "Transatel export 2026-12-01" \
        [--pool UK-SWAP-1] [--dry-run]

See apps/sim_change/services/stock_import.py for the columns and rules.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.sim_change.services.stock_import import StockImportError, import_swap_stock


class Command(BaseCommand):
    help = "Import Swap SIM stock (Change SIM type). Nothing is saved unless every row is valid."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--source", required=True, help="Where this stock list came from.")
        parser.add_argument("--pool", default="", help="Supplier pool ID for rows that don't give one.")
        parser.add_argument("--dry-run", action="store_true", help="Validate only; save nothing.")

    def handle(self, *args, **options):
        path = Path(options["csv_path"])
        if not path.is_file():
            raise CommandError(f"No such file: {path}")
        try:
            result = import_swap_stock(
                path.read_bytes(), source=options["source"], pool=options["pool"], dry_run=options["dry_run"],
            )
        except StockImportError as exc:
            raise CommandError(str(exc)) from exc

        if result.problems:
            for p in result.problems[:50]:
                self.stdout.write(self.style.WARNING(f"  row {p.row} {p.field}: {p.rule}"))
            raise CommandError(f"Not imported: {len(result.problems)} problem(s) in {result.row_count} row(s).")

        verb = "Would import" if result.dry_run else "Imported"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {result.row_count} row(s): {result.created} new, {result.updated} updated."
        ))
