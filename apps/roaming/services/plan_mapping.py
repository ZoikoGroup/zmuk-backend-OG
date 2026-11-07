"""Importing the plan-to-tariff mapping CSV (Engineering Decision v2 §4 B, §5).

Header (exact, agreed production format):
    zoiko_plan_name, term, plan_sku, plan_version, effective_from, effective_to, notes

Unlike the register, a mapping file is all-or-nothing: every row is checked
first and the file is applied only when there are no problems, so a
customer's line is never half-mapped. Rows identical to an existing mapping
are skipped, so re-uploading the same data is harmless. Deleting the
`PlanMappingImport` batch removes exactly the mappings it created.

`effective_from` / `effective_to` are the line ACTIVATION window for this
version (customers keep the terms they joined on), not the charge dates.
"""

from dataclasses import dataclass, field
from datetime import date

from django.db import transaction
from django.db.models import Q

from apps.plans.models import Plan

from ..models import PlanMappingImport, PlanTariffMap, RegisterEntry, RegisterVersion
from .importer import (
    CONTRACT_DURATIONS, PRODUCT_CATALOGUE, checksum, is_blank_row, is_example, parse_date, read_csv,
    windows_overlap,
)

COLUMNS = ("zoiko_plan_name", "term", "plan_sku", "plan_version", "effective_from", "effective_to", "notes")
REQUIRED_COLUMNS = ("zoiko_plan_name", "plan_sku", "effective_from")
BRAND_PREFIX = "zoiko "


class PlanMappingImportError(Exception):
    """The file as a whole can't be read (nothing is saved)."""


@dataclass
class MappingProblem:
    row: int
    plan_name: str
    field: str
    rule: str
    owner: str = PRODUCT_CATALOGUE


@dataclass
class MappingResult:
    row_count: int = 0
    created: int = 0
    skipped: int = 0
    problems: list[MappingProblem] = field(default_factory=list)
    batch: PlanMappingImport | None = None
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class _Row:
    number: int
    plan_name: str
    plan: Plan | None
    term: str
    plan_sku: str
    plan_version: str
    effective_from: date | None
    effective_to: date | None
    notes: str


def _plan_lookup():
    """Lower-cased plan name → plans, so "Zoiko Standard 10GB" also finds "Standard 10GB"."""
    by_name: dict[str, list[Plan]] = {}
    for plan in Plan.objects.all():
        by_name.setdefault(plan.name.strip().lower(), []).append(plan)

    def find(name: str) -> list[Plan]:
        key = name.strip().lower()
        found = by_name.get(key, [])
        if not found and key.startswith(BRAND_PREFIX):
            found = by_name.get(key[len(BRAND_PREFIX):], [])
        return found

    return find


def _register_tariffs() -> set[tuple[str, str]]:
    """(plan_sku, plan_version) pairs present as resolved rows in an active or draft register."""
    return set(
        RegisterEntry.objects.filter(
            version__status__in=[RegisterVersion.Status.ACTIVE, RegisterVersion.Status.DRAFT],
            row_status=RegisterEntry.RowStatus.RESOLVED,
        ).exclude(plan_sku="").values_list("plan_sku", "plan_version").distinct()
    )


def _parse(reader, problems: list[MappingProblem]) -> list[_Row]:
    find_plan = _plan_lookup()
    tariffs = _register_tariffs()
    rows = []
    for number, raw in enumerate(reader, start=2):  # row 1 is the header
        if is_blank_row(raw):
            continue
        get = lambda name: (raw.get(name) or "").strip()  # noqa: E731
        name = get("zoiko_plan_name")

        def problem(field_name, rule):
            problems.append(MappingProblem(number, name, field_name, rule))

        plan = None
        if not name:
            problem("zoiko_plan_name", "zoiko_plan_name is blank")
        else:
            matches = find_plan(name)
            if len(matches) == 1:
                plan = matches[0]
            elif not matches:
                problem("zoiko_plan_name", f"no plan called '{name}' in the plan catalogue")
            else:
                problem("zoiko_plan_name", f"'{name}' matches {len(matches)} plans; names must be unique")

        term = get("term").upper()
        if term and term not in CONTRACT_DURATIONS:
            problem("term", f"term '{term}' is not one of {', '.join(CONTRACT_DURATIONS)} (blank = all terms)")

        sku, version = get("plan_sku"), get("plan_version")
        if not sku:
            problem("plan_sku", "plan_sku is blank")
        elif is_example(sku):
            problem("plan_sku", "example/test SKU, not an approved production tariff")
        elif (sku, version) not in tariffs:
            problem("plan_sku", f"plan_sku '{sku}' version '{version}' is not in an active or draft register; "
                                "import the register first")

        dates = {}
        for name_ in ("effective_from", "effective_to"):
            value = get(name_)
            dates[name_] = None
            if not value:
                if name_ == "effective_from":
                    problem(name_, "effective_from is blank")
                continue
            try:
                dates[name_] = parse_date(value)
            except ValueError:
                problem(name_, f"{name_} '{value}' is not a YYYY-MM-DD date")
        if dates["effective_from"] and dates["effective_to"] and dates["effective_to"] < dates["effective_from"]:
            problem("effective_to", "effective_to is before effective_from")

        rows.append(_Row(number, name, plan, term, sku, version, dates["effective_from"], dates["effective_to"],
                         get("notes")[:255]))
    return rows


def _identical(row: _Row, existing: PlanTariffMap) -> bool:
    return (existing.plan_sku, existing.plan_version, existing.effective_from, existing.effective_to) == (
        row.plan_sku, row.plan_version, row.effective_from, row.effective_to)


def _check_overlaps(rows: list[_Row], problems: list[MappingProblem]) -> list[_Row]:
    """Flag windows that overlap for the same plan + term; return the rows that are new."""
    new_rows = []
    seen: dict[tuple, list[_Row]] = {}
    for row in rows:
        if row.plan is None or row.effective_from is None:
            continue
        existing = PlanTariffMap.objects.filter(plan=row.plan, term=row.term).filter(
            Q(effective_to__isnull=True) | Q(effective_to__gte=row.effective_from)
        )
        clash = None
        duplicate = False
        for mapping in existing:
            if not windows_overlap(row.effective_from, row.effective_to, mapping.effective_from, mapping.effective_to):
                continue
            if _identical(row, mapping):
                duplicate = True
            else:
                clash = f"overlaps existing mapping {mapping.plan_sku} {mapping.plan_version} " \
                        f"from {mapping.effective_from}"
                break
        for other in seen.get((row.plan.pk, row.term), []):
            if windows_overlap(row.effective_from, row.effective_to, other.effective_from, other.effective_to):
                clash = f"overlaps row {other.number} for the same plan and term"
                break
        if clash:
            problems.append(MappingProblem(row.number, row.plan_name, "effective_from", clash))
        seen.setdefault((row.plan.pk, row.term), []).append(row)
        if not duplicate:
            new_rows.append(row)
    return new_rows


def import_plan_mapping(raw: bytes, *, file_name: str = "", notes: str = "", user=None,
                        dry_run: bool = False) -> MappingResult:
    """Validate the whole file; apply it only when there are no problems and not `dry_run`."""
    digest = checksum(raw)
    previous = PlanMappingImport.objects.filter(checksum=digest).first()
    if previous:
        raise PlanMappingImportError(f"This file was already imported on {previous.imported_at:%Y-%m-%d %H:%M}.")
    reader = read_csv(raw, REQUIRED_COLUMNS, PlanMappingImportError)

    problems: list[MappingProblem] = []
    rows = _parse(reader, problems)
    if not rows:
        raise PlanMappingImportError("The file has a header but no data rows.")
    new_rows = _check_overlaps(rows, problems)

    result = MappingResult(row_count=len(rows), problems=problems, dry_run=dry_run,
                           created=len(new_rows), skipped=len(rows) - len(new_rows))
    if problems or dry_run:
        if problems:
            result.created = result.skipped = 0
        return result

    with transaction.atomic():
        batch = PlanMappingImport.objects.create(
            checksum=digest, file_name=file_name[:255], notes=notes, imported_by=user,
            row_count=result.row_count, created_count=result.created, skipped_count=result.skipped,
        )
        PlanTariffMap.objects.bulk_create([
            PlanTariffMap(
                plan=row.plan, term=row.term, plan_sku=row.plan_sku, plan_version=row.plan_version,
                effective_from=row.effective_from, effective_to=row.effective_to, notes=row.notes,
                import_batch=batch, source_row=row.number,
            )
            for row in new_rows
        ])
    result.batch = batch
    return result
