"""Import the plan-to-tariff mapping CSV (same as the admin upload).

The whole file is validated first and applied only if every row is valid.

Usage:
    python manage.py import_plan_tariff_map plan_mapping.csv [--dry-run] [--report problems.csv]
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.roaming.services import plan_mapping, reports


class Command(BaseCommand):
    help = "Import a plan-to-tariff mapping CSV."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--notes", default="")
        parser.add_argument("--dry-run", action="store_true", help="Validate only; save nothing.")
        parser.add_argument("--report", help="Write the row-level problem report to this CSV file.")

    def handle(self, *args, **options):
        path = Path(options["csv_path"])
        if not path.is_file():
            raise CommandError(f"No such file: {path}")
        try:
            result = plan_mapping.import_plan_mapping(
                path.read_bytes(), file_name=path.name, notes=options["notes"], dry_run=options["dry_run"],
            )
        except plan_mapping.PlanMappingImportError as exc:
            raise CommandError(str(exc)) from exc

        if options["report"]:
            with open(options["report"], "w", encoding="utf-8", newline="") as stream:
                reports.write_report(stream, reports.MAPPING_REPORT_COLUMNS,
                                     reports.mapping_report_rows(result.problems))
            self.stdout.write(f"Problem report written to {options['report']}.")

        if result.problems:
            for p in result.problems[:20]:
                self.stdout.write(self.style.WARNING(f"  row {p.row} ({p.plan_name}) {p.field}: {p.rule}"))
            raise CommandError(f"Not applied: {len(result.problems)} problem(s) in {result.row_count} row(s).")

        verb = "Would create" if result.dry_run else "Created"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {result.created} mapping(s); {result.skipped} already present."
        ))
