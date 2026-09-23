"""
Shared CSV-import logic for SIM inventory.

Used by:
  • apps/malima/management/commands/import_sims.py  (CLI)
  • apps/malima/admin.py                            (admin button)

The CSV format is the Malima/Orange daily export (CDFLTDLY_*.csv):
semicolon-delimited, UTF-8-BOM, quoted, 62 columns. Column names map
1:1 onto SimInventory fields, so no normalisation/aliasing is needed —
we just copy each column straight onto the model.

Two columns get special handling:
  • simStatus / simProfile — informational only; they don't drive the
    `inventory` (INSTOCK/PENDING/OUTOFSTOCK) state. A new row is always
    INSTOCK unless --status overrides it.
  • Date fields — parsed as ISO 8601 with timezone offset; bad values
    become NULL rather than raising.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Iterable, Optional

from django.db import transaction
from django.utils.dateparse import parse_datetime

from .models import SimInventory


# Every column in the daily export → matching model field. This is the full
# 62-column list. If the carrier adds new columns in future, just add them
# here and on the model.
COLUMN_MAP: dict[str, str] = {
    "simCardID":                       "simCardID",
    "simIccid":                        "simIccid",
    "msisdnData":                      "msisdnData",
    "msisdnVoice":                     "msisdnVoice",
    "imsi":                            "imsi",
    "deviceImei":                      "deviceImei",
    "simStatus":                       "simStatus",
    "subIdentifier":                   "subIdentifier",
    "subscriberName":                  "subscriberName",
    "subCreationDate":                 "subCreationDate",
    "subOwner":                        "subOwner",
    "customerOrderReference":          "customerOrderReference",
    "simLastStatusRefreshDate":        "simLastStatusRefreshDate",
    "simLastStatusChangeDate":         "simLastStatusChangeDate",
    "subIsConnectionDate":             "subIsConnectionDate",
    "simSuspensionReason":             "simSuspensionReason",
    "groupingCriteriaOne":             "groupingCriteriaOne",
    "groupListOne":                    "groupListOne",
    "groupingCriteriaTwo":             "groupingCriteriaTwo",
    "groupListTwo":                    "groupListTwo",
    "groupingCriteriaThree":           "groupingCriteriaThree",
    "groupListThree":                  "groupListThree",
    "deviceUniqueIdentifier":          "deviceUniqueIdentifier",
    "deviceSerialNumber":              "deviceSerialNumber",
    "machineSerialNumber":             "machineSerialNumber",
    "machineName":                     "machineName",
    "deviceLocation":                  "deviceLocation",
    "deviceCategory":                  "deviceCategory",
    "deviceDescription":               "deviceDescription",
    "deviceAddress":                   "deviceAddress",
    "deviceHolder":                    "deviceHolder",
    "deviceCommunicationModuleBrand":  "deviceCommunicationModuleBrand",
    "deviceCommunicationModuleModel":  "deviceCommunicationModuleModel",
    "deviceUpdateDate":                "deviceUpdateDate",
    "deviceContactName":               "deviceContactName",
    "deviceContactEmail":              "deviceContactEmail",
    "deviceContactPhone":              "deviceContactPhone",
    "machineDescription":              "machineDescription",
    "machineUpdateDate":               "machineUpdateDate",
    "simCardType":                     "simCardType",
    "simProfile":                      "simProfile",
    "networkConnectionStatus":         "networkConnectionStatus",
    "networkConnectionLastUpdate":     "networkConnectionLastUpdate",
    "networkConnectionCountry":        "networkConnectionCountry",
    "networkConnectionOperator":       "networkConnectionOperator",
    "networkConnectionRadioType":      "networkConnectionRadioType",
    "networkConnectionLastInteraction": "networkConnectionLastInteraction",
    "networkConnectionAPN":            "networkConnectionAPN",
    "tariffPlanCode":                  "tariffPlanCode",
    "tariffPlanLabel":                 "tariffPlanLabel",
    "activeOptionCodes":               "activeOptionCodes",
    "activeOptionLabels":              "activeOptionLabels",
    "eID":                             "eID",
    "simProfileStatus":                "simProfileStatus",
    "simCapacities":                   "simCapacities",
    # The carrier also has these columns we don't store yet — listed here
    # so we know to look at them if a feature ever needs them. Currently
    # silently dropped:
    #   activationCode, standardisedFullName,
    #   module2GCompatibility, module3GCompatibility,
    #   module4GCompatibility, module4GVoLTECompatibility,
    #   module5GCompatibility
}

# Columns that need to be parsed as datetimes. Anything else is a string.
DATETIME_COLUMNS = {
    "subCreationDate", "simLastStatusRefreshDate", "simLastStatusChangeDate",
    "subIsConnectionDate", "deviceUpdateDate", "machineUpdateDate",
    "networkConnectionLastUpdate", "networkConnectionLastInteraction",
}

# Required for a row to be considered importable.
REQUIRED_COLUMNS = ["simIccid", "imsi"]


# ── Header normalisation ────────────────────────────────────────────────────

def _norm_header(h: str) -> str:
    return (h or "").lstrip("\ufeff").strip()


def build_header_map(fieldnames: Iterable[str]) -> dict[str, str]:
    """Return {model_field → actual_csv_header} for whatever's in the file."""
    out: dict[str, str] = {}
    norm = {_norm_header(h): h for h in (fieldnames or [])}
    for csv_col, model_field in COLUMN_MAP.items():
        if csv_col in norm:
            out[model_field] = norm[csv_col]
    return out


# ── Value normalisation ──────────────────────────────────────────────────────

def _clean(value: Optional[str]) -> Optional[str]:
    """Trim and return None for empty."""
    if value is None:
        return None
    v = value.strip()
    return v or None


def _clean_phone(value: Optional[str]) -> Optional[str]:
    """Drop '+' and spaces from MSISDN/phone fields. Empty → None."""
    if value is None:
        return None
    v = "".join(ch for ch in value if ch.isdigit())
    return v or None


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """ISO 8601 with offset (`2026-04-01T06:12:41+02:00`). None on bad input."""
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    try:
        return parse_datetime(v)
    except (ValueError, TypeError):
        return None


# ── Delimiter detection ──────────────────────────────────────────────────────

def detect_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        return dialect.delimiter
    except csv.Error:
        return ";" if sample.count(";") > sample.count(",") else ","


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class ImportResult:
    delimiter: str = ","
    columns_detected: list = field(default_factory=list)
    columns_missing: list = field(default_factory=list)
    created: int = 0
    updated: int = 0
    skipped: int = 0
    invalid: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        return self.created + self.updated + self.skipped + self.invalid

    def summary_text(self) -> str:
        return " · ".join([
            f"Created: {self.created}",
            f"Updated: {self.updated}",
            f"Skipped (existing): {self.skipped}",
            f"Rejected (invalid): {self.invalid}",
        ])


# ── Core import ──────────────────────────────────────────────────────────────

def import_sims_from_stream(
    stream: IO[str],
    *,
    delimiter: str = "",
    dry_run: bool = False,
    update_existing: bool = False,
    limit: int = 0,
) -> ImportResult:
    """
    Read CSV from a text stream and import rows into SimInventory.

    `stream` must be opened in text mode with utf-8-sig encoding (so the BOM
    is stripped). For file uploads, wrap the UploadedFile via
    `io.StringIO(uploaded.read().decode("utf-8-sig"))`.
    """
    result = ImportResult()

    # Detect delimiter from a sample, rewind, then parse for real.
    if not delimiter:
        sample = stream.read(8192)
        stream.seek(0)
        delimiter = detect_delimiter(sample)
    result.delimiter = delimiter

    reader = csv.DictReader(stream, delimiter=delimiter)
    if not reader.fieldnames:
        result.warnings.append("CSV has no header row.")
        return result

    # The carrier's CSV has the BOM on the first column header. Strip it.
    header_map = build_header_map(reader.fieldnames)
    result.columns_detected = sorted(header_map.keys())
    result.columns_missing = [
        f for f in REQUIRED_COLUMNS if f not in header_map
    ]
    if result.columns_missing:
        result.warnings.append(
            f"CSV missing required column(s): {result.columns_missing}. "
            f"Expected columns include: simIccid, imsi."
        )
        return result

    # Pull both lookup sets so we can dedupe on either unique field.
    existing_by_iccid: dict[str, int] = dict(
        SimInventory.objects.values_list("simIccid", "id")
    )
    existing_imsis: set[str] = set(
        SimInventory.objects.values_list("imsi", flat=True)
    )

    seen_iccids_in_file: set[str] = set()
    seen_imsis_in_file: set[str] = set()
    new_rows: list[SimInventory] = []
    update_rows: list[tuple[int, dict]] = []   # (pk, field_dict)

    for i, row in enumerate(reader, start=2):
        if limit and (i - 1) > limit:
            break

        # Read every mapped column into a dict.
        values: dict[str, object] = {}
        for model_field, csv_header in header_map.items():
            raw = row.get(csv_header, "")
            if model_field in DATETIME_COLUMNS:
                values[model_field] = _parse_datetime(raw)
            elif model_field in ("msisdnData", "msisdnVoice", "deviceContactPhone"):
                values[model_field] = _clean_phone(raw)
            else:
                values[model_field] = _clean(raw)

        iccid = values.get("simIccid")
        imsi = values.get("imsi")
        if not iccid or not imsi:
            result.invalid += 1
            continue

        # In-file deduplication (rare, but possible if the export had a glitch).
        if iccid in seen_iccids_in_file:
            result.skipped += 1
            continue
        seen_iccids_in_file.add(iccid)
        if imsi in seen_imsis_in_file:
            result.warnings.append(
                f"Row {i}: imsi {imsi} appears twice in this file — skipping."
            )
            result.skipped += 1
            continue
        seen_imsis_in_file.add(imsi)

        # Cross-row IMSI clash against a row that has a different ICCID.
        if imsi in existing_imsis:
            existing_id_for_iccid = existing_by_iccid.get(iccid)
            if existing_id_for_iccid is None:
                # Some other row has this IMSI — can't import.
                result.warnings.append(
                    f"Row {i}: imsi {imsi} already attached to a different "
                    f"ICCID in DB — skipping."
                )
                result.skipped += 1
                continue

        if iccid in existing_by_iccid:
            if not update_existing:
                result.skipped += 1
                continue
            update_rows.append((existing_by_iccid[iccid], values))
        else:
            new_rows.append(SimInventory(**values))

    if dry_run:
        result.created = len(new_rows)
        result.updated = len(update_rows)
        return result

    with transaction.atomic():
        if new_rows:
            SimInventory.objects.bulk_create(new_rows, batch_size=500)
        for pk, fields in update_rows:
            # Don't touch the `inventory` state on update — that's our internal
            # bookkeeping, not something the carrier's CSV should overwrite.
            SimInventory.objects.filter(pk=pk).update(**fields)

    result.created = len(new_rows)
    result.updated = len(update_rows)
    return result


def import_sims_from_path(path, **kwargs) -> ImportResult:
    """Convenience wrapper used by the CLI."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return import_sims_from_stream(fh, **kwargs)


def import_sims_from_upload(uploaded_file, **kwargs) -> ImportResult:
    """
    Convenience wrapper for Django's UploadedFile (admin form).
    Strips BOM via utf-8-sig decode.
    """
    text = uploaded_file.read().decode("utf-8-sig")
    return import_sims_from_stream(io.StringIO(text), **kwargs)
