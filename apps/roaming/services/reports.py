"""Machine-readable rejection reports (Engineering Decision v2, Appendix B).

One line per problem: row number, original source key, field, failed rule
and correction owner, so each owner can fix their rows in the source system.
"""

import csv

from ..models import RegisterEntry, RegisterVersion

REGISTER_REPORT_COLUMNS = (
    "row_number", "source_system", "source_record_id", "plan_sku", "plan_version", "contract_duration",
    "territory_code", "effective_from", "field", "failed_rule", "correction_owner",
)
MAPPING_REPORT_COLUMNS = ("row_number", "zoiko_plan_name", "field", "failed_rule", "correction_owner")

# A cell starting with one of these is run as a formula by Excel/Sheets.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def register_report_rows(version: RegisterVersion):
    entries = version.entries.filter(row_status=RegisterEntry.RowStatus.UNRESOLVED).order_by("row_number")
    for entry in entries:
        problems = entry.problems or [{"field": "", "rule": entry.unresolved_reason, "owner": ""}]
        for problem in problems:
            yield (
                entry.row_number, entry.source_system, entry.source_record_id, entry.plan_sku, entry.plan_version,
                entry.contract_duration, entry.territory_code, entry.effective_from or "",
                problem.get("field", ""), problem.get("rule", ""), problem.get("owner", ""),
            )


def mapping_report_rows(problems):
    for p in problems:
        yield p.row, p.plan_name, p.field, p.rule, p.owner


def write_report(stream, columns, rows) -> None:
    writer = csv.writer(stream)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_safe(value) for value in row])
