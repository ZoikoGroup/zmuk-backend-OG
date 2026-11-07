"""Importing the destination-and-plan register from Product/Operations (ROAM-005 §02 A).

The file is a CSV whose header uses the §04 field names (see `COLUMNS`).
Every row is kept: a row that can't be trusted (unknown territory, bad
number, missing date…) is stored as UNRESOLVED with the reason, so the
mismatch list stays visible instead of silently disappearing. Quotes never
use unresolved rows.

`included_voice_minutes` / `included_sms` accept "unlimited". Leave
`plan_sku` blank for the published general rates of a destination.

Imports always create a DRAFT version; `activate()` makes it live.
"""

import csv
import hashlib
import io
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from ..models import Destination, RegisterEntry, RegisterVersion

REQUIRED_COLUMNS = ("territory_code", "effective_from", "source_system", "source_record_id")

COLUMNS = (
    "plan_sku", "plan_version", "contract_duration", "customer_tariff_id",
    "territory_code", "rating_zone", "supplier_availability", "inclusion",
    "included_data_mb", "included_voice_minutes", "included_sms", "fair_use_terms",
    "rate_data_per_mb", "rate_voice_to_uk_per_min", "rate_voice_local_per_min",
    "rate_voice_incoming_per_min", "rate_sms", "rate_mms", "rate_units_rounding",
    "vat_basis", "currency", "exhaustion_action", "applicable_spend_control",
    "effective_from", "effective_to", "source_system", "source_record_id", "validation_date",
)

RATE_FIELDS = (
    "rate_data_per_mb", "rate_voice_to_uk_per_min", "rate_voice_local_per_min",
    "rate_voice_incoming_per_min", "rate_sms", "rate_mms",
)

TEXT_FIELDS = (
    "plan_sku", "plan_version", "contract_duration", "customer_tariff_id", "rating_zone",
    "fair_use_terms", "rate_units_rounding", "vat_basis", "applicable_spend_control",
    "source_system", "source_record_id",
)

CHOICE_FIELDS = {
    "supplier_availability": RegisterEntry.Availability,
    "inclusion": RegisterEntry.Inclusion,
    "exhaustion_action": RegisterEntry.Exhaustion,
}


class RegisterImportError(Exception):
    """The file as a whole can't be imported (nothing is saved)."""


def checksum(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _rows(raw: bytes):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RegisterImportError("The file must be UTF-8 encoded CSV.") from exc
    if not text.strip():
        raise RegisterImportError("The file is empty.")
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise RegisterImportError(f"Missing required column(s): {', '.join(missing)}.")
    reader.fieldnames = header
    return reader


def _parse_row(row: dict, destinations: dict) -> tuple[dict, list[str]]:
    """Return (field values, problems) for one CSV row."""
    values, problems = {}, []
    get = lambda name: (row.get(name) or "").strip()  # noqa: E731

    for name in TEXT_FIELDS:
        values[name] = get(name)

    code = get("territory_code").upper()
    values["territory_code"] = code
    values["destination"] = destinations.get(code)
    if not code:
        problems.append("territory_code is blank")
    elif values["destination"] is None:
        problems.append(f"unknown territory_code '{code}'")

    for name, choices in CHOICE_FIELDS.items():
        raw = get(name).lower()
        if not raw:
            values[name] = choices.UNKNOWN
        elif raw in choices.values:
            values[name] = raw
        else:
            values[name] = choices.UNKNOWN
            problems.append(f"{name} '{raw}' is not one of {', '.join(choices.values)}")

    values["currency"] = (get("currency") or "GBP").upper()[:3]

    raw_data = get("included_data_mb")
    values["included_data_mb"] = None
    if raw_data:
        if raw_data.isdigit():
            values["included_data_mb"] = int(raw_data)
        else:
            problems.append(f"included_data_mb '{raw_data}' is not a whole number")

    for name, unlimited_flag in (("included_voice_minutes", "voice_unlimited"), ("included_sms", "sms_unlimited")):
        raw = get(name).lower()
        values[name], values[unlimited_flag] = None, False
        if raw == "unlimited":
            values[unlimited_flag] = True
        elif raw.isdigit():
            values[name] = int(raw)
        elif raw:
            problems.append(f"{name} '{raw}' is not a whole number or 'unlimited'")

    for name in RATE_FIELDS:
        raw = get(name).replace("£", "")
        values[name] = None
        if raw:
            try:
                amount = Decimal(raw)
            except InvalidOperation:
                problems.append(f"{name} '{raw}' is not a number")
                continue
            if amount < 0:
                problems.append(f"{name} is negative")
            else:
                values[name] = amount

    for name in ("effective_from", "effective_to", "validation_date"):
        raw = get(name)
        values[name] = None
        if not raw:
            if name == "effective_from":
                problems.append("effective_from is blank")
            continue
        try:
            values[name] = date.fromisoformat(raw)
        except ValueError:
            problems.append(f"{name} '{raw}' is not a YYYY-MM-DD date")
    if values["effective_from"] and values["effective_to"] and values["effective_to"] < values["effective_from"]:
        problems.append("effective_to is before effective_from")

    if not values["source_system"] or not values["source_record_id"]:
        problems.append("source_system and source_record_id are required for data lineage")

    return values, problems


@transaction.atomic
def import_register(raw: bytes, *, label: str, validation_date: date, source_system: str = "",
                    notes: str = "", user=None) -> RegisterVersion:
    """Create a DRAFT `RegisterVersion` from a CSV export. Raises RegisterImportError."""
    digest = checksum(raw)
    existing = RegisterVersion.objects.filter(checksum=digest).first()
    if existing:
        raise RegisterImportError(f"This file was already imported as '{existing.label}'.")
    if RegisterVersion.objects.filter(label=label).exists():
        raise RegisterImportError(f"A register version called '{label}' already exists.")

    reader = _rows(raw)
    destinations = {d.code: d for d in Destination.objects.all()}
    version = RegisterVersion.objects.create(
        label=label, checksum=digest, validation_date=validation_date,
        source_system=source_system, notes=notes, imported_by=user,
    )

    entries, seen = [], set()
    for row_number, row in enumerate(reader, start=2):  # row 1 is the header
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue
        values, problems = _parse_row(row, destinations)
        key = (values["plan_sku"], values["plan_version"], values["contract_duration"],
               values["territory_code"], values["effective_from"])
        if key in seen:
            problems.append("duplicate of an earlier row (same plan, territory and effective_from)")
        seen.add(key)
        entries.append(RegisterEntry(
            version=version, row_number=row_number, **values,
            row_status=RegisterEntry.RowStatus.UNRESOLVED if problems else RegisterEntry.RowStatus.RESOLVED,
            unresolved_reason="; ".join(problems),
        ))

    if not entries:
        raise RegisterImportError("The file has a header but no data rows.")
    RegisterEntry.objects.bulk_create(entries)
    version.row_count = len(entries)
    version.unresolved_count = sum(e.row_status == RegisterEntry.RowStatus.UNRESOLVED for e in entries)
    version.save(update_fields=["row_count", "unresolved_count"])
    return version


@transaction.atomic
def activate(version: RegisterVersion, user=None) -> RegisterVersion:
    """Make `version` the live register; the previous active one is retired."""
    if version.status == RegisterVersion.Status.ACTIVE:
        return version
    RegisterVersion.objects.filter(status=RegisterVersion.Status.ACTIVE).update(
        status=RegisterVersion.Status.RETIRED
    )
    version.status = RegisterVersion.Status.ACTIVE
    version.activated_by = user
    version.activated_at = timezone.now()
    version.save(update_fields=["status", "activated_by", "activated_at"])
    return version
