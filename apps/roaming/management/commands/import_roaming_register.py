"""Import a roaming register CSV as a draft version (same as the admin upload).

Usage:
    python manage.py import_roaming_register register.csv --label 2026-11-parity-r1 \
        --validation-date 2026-11-09 --source-system "Billing rating export" \
        [--report rejections.csv] [--activate]
"""

from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.roaming.services import importer, reports


class Command(BaseCommand):
    help = "Import a roaming destination-and-plan register CSV."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--label", required=True)
        parser.add_argument("--validation-date", required=True, type=date.fromisoformat)
        parser.add_argument("--source-system", default="")
        parser.add_argument("--notes", default="")
        parser.add_argument("--report", help="Write the row-level rejection report to this CSV file.")
        parser.add_argument("--activate", action="store_true",
                            help="Make this version live immediately (only if it has no unresolved rows).")

    def handle(self, *args, **options):
        path = Path(options["csv_path"])
        if not path.is_file():
            raise CommandError(f"No such file: {path}")
        try:
            version = importer.import_register(
                path.read_bytes(), label=options["label"], validation_date=options["validation_date"],
                source_system=options["source_system"], notes=options["notes"],
            )
        except importer.RegisterImportError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"Imported '{version.label}': {version.row_count} rows, "
                          f"{version.unresolved_count} unresolved.")
        for entry in version.entries.filter(row_status="unresolved")[:20]:
            self.stdout.write(self.style.WARNING(f"  row {entry.row_number}: {entry.unresolved_reason}"))
        if options["report"]:
            with open(options["report"], "w", encoding="utf-8", newline="") as stream:
                reports.write_report(stream, reports.REGISTER_REPORT_COLUMNS, reports.register_report_rows(version))
            self.stdout.write(f"Rejection report written to {options['report']}.")

        if options["activate"]:
            if version.unresolved_count:
                raise CommandError("Not activated: fix or accept the unresolved rows, then activate in the admin.")
            importer.activate(version)
            self.stdout.write(self.style.SUCCESS(f"'{version.label}' is now the active register."))
