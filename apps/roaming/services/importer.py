"""Importing the destination-and-plan register from Product/Operations (ROAM-005 §02 A).

The file is a CSV whose header uses the §04 field names (see `COLUMNS`); the
28-column order is the agreed production import format (Engineering Decision
v2 §5). Every row is kept: a row that can't be trusted (unknown territory,
bad number, missing date, example data, overlapping window…) is stored as
UNRESOLVED with each problem recorded as {field, rule, owner}, so the
rejection report stays visible instead of rows silently disappearing.
Quotes never use unresolved rows.

Blank always means UNKNOWN, never zero, GBP, included or allowed.
`included_voice_minutes` / `included_sms` accept "unlimited". Leave
`plan_sku` blank only for approved published general rates.

Imports always create a DRAFT version; `activate()` makes it live.
"""

import csv
import hashlib
import io
import re
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
    "fair_use_terms", "rate_units_rounding", "applicable_spend_control",
    "source_system", "source_record_id",
)

CHOICE_FIELDS = {
    "supplier_availability": RegisterEntry.Availability,
    "inclusion": RegisterEntry.Inclusion,
    "exhaustion_action": RegisterEntry.Exhaustion,
}

VAT_BASES = ("inclusive", "exclusive")
CONTRACT_DURATIONS = ("30D", "12M", "24M")

# Rates are stored as Decimal(10, 4). Anything finer or larger is rejected
# rather than rounded (Decision v2 §5 "Rate precision").
RATE_DECIMAL_PLACES = 4
RATE_MAX = Decimal("1000000")
CURRENCY_SYMBOLS = ("£", "$", "€")

# Illustrative rows from the first template; never production data (Decision v2 §5, App. A01).
EXAMPLE_SKU_PREFIX = "EXAMPLE"
EXAMPLE_SOURCE_IDS = {"RT-1042", "TS-2025-CH", "WP-ROAMING-US", "AUR-BAR-MAR"}

# Who corrects a problem in each field (Decision v2 §3–4 ownership model).
PRODUCT_CATALOGUE = "Product Catalogue"
BILLING_FINANCE = "Billing/Finance"
SUPPLIER_OPS = "Transatel/Auriga Integration Ops"
PRODUCT_OPS = "Product/Commercial Operations"
FIELD_OWNERS = {
    **dict.fromkeys(("plan_sku", "plan_version", "contract_duration", "customer_tariff_id"), PRODUCT_CATALOGUE),
    **dict.fromkeys(("territory_code", "supplier_availability"), SUPPLIER_OPS),
    **dict.fromkeys(("rating_zone", "rate_units_rounding", "vat_basis", "currency", *RATE_FIELDS), BILLING_FINANCE),
}


def owner_for(field: str) -> str:
    return FIELD_OWNERS.get(field, PRODUCT_OPS)


class RegisterImportError(Exception):
    """The file as a whole can't be imported (nothing is saved)."""


def checksum(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_csv(raw: bytes, required_columns, error_class=RegisterImportError) -> csv.DictReader:
    """UTF-8 CSV (comma or semicolon) with lower-cased headers; raises if a required column is missing."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise error_class("The file must be UTF-8 encoded CSV.") from exc
    if not text.strip():
        raise error_class("The file is empty.")
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in required_columns if c not in header]
    if missing:
        raise error_class(f"Missing required column(s): {', '.join(missing)}.")
    reader.fieldnames = header
    return reader


def is_blank_row(row: dict) -> bool:
    return not any((v or "").strip() for v in row.values() if isinstance(v, str))


def is_example(plan_sku: str, source_record_id: str = "") -> bool:
    return plan_sku.upper().startswith(EXAMPLE_SKU_PREFIX) or source_record_id.upper() in EXAMPLE_SOURCE_IDS


def parse_date(raw: str) -> date:
    """YYYY-MM-DD only. Raises ValueError."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ValueError(raw)
    return date.fromisoformat(raw)


def parse_rate(raw: str) -> tuple[Decimal | None, str | None]:
    """(amount, problem). Plain non-negative decimal, at most 4 places, no currency symbol."""
    if any(symbol in raw for symbol in CURRENCY_SYMBOLS):
        return None, f"'{raw}' contains a currency symbol; enter a plain decimal such as 0.0160"
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None, f"'{raw}' is not a number"
    if not amount.is_finite():
        return None, f"'{raw}' is not a number"
    if amount < 0:
        return None, "is negative"
    if amount.as_tuple().exponent < -RATE_DECIMAL_PLACES:
        return None, (f"'{raw}' has more than {RATE_DECIMAL_PLACES} decimal places; it was not rounded "
                      "(a higher-precision schema needs a controlled version change)")
    if amount >= RATE_MAX:
        return None, f"'{raw}' is too large"
    return amount, None


def _parse_row(row: dict, destinations: dict) -> tuple[dict, list[dict]]:
    """Return (field values, problems) for one CSV row."""
    values, problems = {}, []
    get = lambda name: (row.get(name) or "").strip()  # noqa: E731

    def problem(field, rule):
        problems.append({"field": field, "rule": rule, "owner": owner_for(field)})

    for name in TEXT_FIELDS:
        values[name] = get(name)

    if is_example(values["plan_sku"], values["source_record_id"]):
        problem("plan_sku" if values["plan_sku"].upper().startswith(EXAMPLE_SKU_PREFIX) else "source_record_id",
                "example/test row, not an approved production tariff record")

    duration = values["contract_duration"].upper()
    values["contract_duration"] = duration
    if duration and duration not in CONTRACT_DURATIONS:
        problem("contract_duration", f"contract_duration '{duration}' is not one of {', '.join(CONTRACT_DURATIONS)}")

    code = get("territory_code").upper()
    values["territory_code"] = code
    values["destination"] = destinations.get(code)
    if not code:
        problem("territory_code", "territory_code is blank")
    elif values["destination"] is None:
        problem("territory_code", f"unknown territory_code '{code}'")

    for name, choices in CHOICE_FIELDS.items():
        raw = get(name).lower()
        if raw in ("", choices.UNKNOWN):
            values[name] = choices.UNKNOWN
        elif raw in choices.values:
            values[name] = raw
        else:
            values[name] = choices.UNKNOWN
            allowed = [v for v in choices.values if v != choices.UNKNOWN]
            problem(name, f"{name} '{raw}' is not one of {', '.join(allowed)} (leave blank if unknown)")

    vat = get("vat_basis").lower()
    values["vat_basis"] = vat if vat in VAT_BASES else ""
    if vat and vat not in VAT_BASES:
        problem("vat_basis", f"vat_basis '{vat}' is not one of {', '.join(VAT_BASES)} (leave blank if unknown)")

    currency = get("currency").upper()
    values["currency"] = currency if re.fullmatch(r"[A-Z]{3}", currency) else ""
    if currency and not values["currency"]:
        problem("currency", f"currency '{currency}' is not a 3-letter ISO code such as GBP")

    raw_data = get("included_data_mb")
    values["included_data_mb"] = None
    if raw_data:
        if raw_data.isdigit():
            values["included_data_mb"] = int(raw_data)
        else:
            problem("included_data_mb", f"included_data_mb '{raw_data}' is not a whole number")

    for name, unlimited_flag in (("included_voice_minutes", "voice_unlimited"), ("included_sms", "sms_unlimited")):
        raw = get(name).lower()
        values[name], values[unlimited_flag] = None, False
        if raw == "unlimited":
            values[unlimited_flag] = True
        elif raw.isdigit():
            values[name] = int(raw)
        elif raw:
            problem(name, f"{name} '{raw}' is not a whole number or 'unlimited'")

    for name in RATE_FIELDS:
        raw = get(name)
        values[name] = None
        if raw:
            amount, rule = parse_rate(raw)
            if rule:
                problem(name, f"{name} {rule}")
            else:
                values[name] = amount

    for name in ("effective_from", "effective_to", "validation_date"):
        raw = get(name)
        values[name] = None
        if not raw:
            if name == "effective_from":
                problem(name, "effective_from is blank")
            continue
        try:
            values[name] = parse_date(raw)
        except ValueError:
            problem(name, f"{name} '{raw}' is not a YYYY-MM-DD date")
    if values["effective_from"] and values["effective_to"] and values["effective_to"] < values["effective_from"]:
        problem("effective_to", "effective_to is before effective_from")

    if not values["source_system"] or not values["source_record_id"]:
        problem("source_record_id", "source_system and source_record_id are required for data lineage")

    return values, problems


def windows_overlap(a_from: date, a_to: date | None, b_from: date, b_to: date | None) -> bool:
    """Inclusive date windows; a blank end means open-ended."""
    return a_from <= (b_to or date.max) and b_from <= (a_to or date.max)


def _flag_overlaps(parsed: list[tuple[int, dict, list[dict]]]) -> None:
    """Rows for the same plan/version/duration/territory must not overlap in time (Decision v2 A09).

    Both rows are flagged: there is no "last row wins".
    """
    groups: dict[tuple, list[tuple[int, dict, list[dict]]]] = {}
    for item in parsed:
        values = item[1]
        if values["effective_from"] is None:
            continue
        key = (values["plan_sku"], values["plan_version"], values["contract_duration"], values["territory_code"])
        groups.setdefault(key, []).append(item)
    for rows in groups.values():
        for i, (row_a, a, problems_a) in enumerate(rows):
            for row_b, b, problems_b in rows[i + 1:]:
                if windows_overlap(a["effective_from"], a["effective_to"], b["effective_from"], b["effective_to"]):
                    rule = "effective window overlaps row {} (same plan, version, contract duration and territory)"
                    for problems, other in ((problems_a, row_b), (problems_b, row_a)):
                        problems.append({"field": "effective_from", "rule": rule.format(other),
                                         "owner": owner_for("effective_from")})


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

    reader = read_csv(raw, REQUIRED_COLUMNS)
    destinations = {d.code: d for d in Destination.objects.all()}

    parsed = []
    for row_number, row in enumerate(reader, start=2):  # row 1 is the header
        if is_blank_row(row):
            continue
        values, problems = _parse_row(row, destinations)
        parsed.append((row_number, values, problems))
    if not parsed:
        raise RegisterImportError("The file has a header but no data rows.")
    _flag_overlaps(parsed)

    version = RegisterVersion.objects.create(
        label=label, checksum=digest, validation_date=validation_date,
        source_system=source_system, notes=notes, imported_by=user,
    )
    entries = [
        RegisterEntry(
            version=version, row_number=row_number, **values,
            row_status=RegisterEntry.RowStatus.UNRESOLVED if problems else RegisterEntry.RowStatus.RESOLVED,
            unresolved_reason="; ".join(p["rule"] for p in problems),
            problems=problems,
        )
        for row_number, values, problems in parsed
    ]
    RegisterEntry.objects.bulk_create(entries)
    version.row_count = len(entries)
    version.unresolved_count = sum(e.row_status == RegisterEntry.RowStatus.UNRESOLVED for e in entries)
    version.save(update_fields=["row_count", "unresolved_count"])
    return version


@transaction.atomic
def activate(version: RegisterVersion, user=None) -> RegisterVersion:
    """Make `version` the live register; the previous active one is retired.

    Re-activating a retired version is the rollback path.
    """
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
