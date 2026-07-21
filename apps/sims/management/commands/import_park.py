from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from sims.csv_import import CsvImportError, import_sims


class Command(BaseCommand):
    help = "Import a Transatel SIM park CSV export (upsert on ICCID)."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Path to the park .csv export")

    def handle(self, *args, **options):
        path = options["csv_path"]
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            raise CommandError(f"Could not read {path}: {exc}")

        try:
            with transaction.atomic():
                result = import_sims(raw)
        except CsvImportError as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(
            f"Created {result['created']}, updated {result['updated']}, "
            f"skipped {result['skipped']}."
        ))
        for err in result["errors"][:20]:
            self.stdout.write(self.style.WARNING(err))
        if len(result["errors"]) > 20:
            self.stdout.write(self.style.WARNING(
                f"...and {len(result['errors']) - 20} more warnings."
            ))
