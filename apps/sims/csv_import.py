"""CSV import for the Transatel SIM park export.

The export is semicolon-delimited, uses ISO-8601 timestamps with a
trailing ``Z``, and contains several always-empty columns. This module
tolerates all of that, maps headers to model fields by name (so column
order and extra columns don't matter), and upserts on ICCID.
"""
import csv
import io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import Sim

# CSV header  ->  model field name
HEADER_MAP = {
    "Serial number": "serial_number",
    "Type of SIM": "type_of_sim",
    "SIM reference": "sim_reference",
    "ICCID": "iccid",
    "IMSI": "imsi",
    "MSISDN": "msisdn",
    "Provisioning status": "provisioning_status",
    "First activation date": "first_activation_date",
    "Last action date": "last_action_date",
    "Subscriber": "subscriber",
    "Company": "company",
    "Reference": "reference",
    "Point of sale": "point_of_sale",
    "Group": "group",
    "Customer account": "customer_account",
    "Provisioning action number": "provisioning_action_number",
    "Subscriber number": "subscriber_number",
    "Service pack": "service_pack",
    "Service profile": "service_profile",
    "Prepaid status": "prepaid_status",
    "Rate plan": "rate_plan",
    "Last IMEI": "last_imei",
    "Last Origin Country": "last_origin_country",
    "Last MNC": "last_mnc",
    "Last MCC": "last_mcc",
    "Last RAT": "last_rat",
    "Data Usage (GB)": "data_usage_gb",
    "Voice Usage": "voice_usage",
    "SMS Usage": "sms_usage",
    "MMS Usage": "mms_usage",
    "Last Destination": "last_destination",
    "Last Seen Date": "last_seen_date",
}

DATE_FIELDS = {"first_activation_date", "last_action_date", "last_seen_date"}
DECIMAL_FIELDS = {"data_usage_gb"}


class CsvImportError(Exception):
    pass


def _parse_datetime(value):
    value = (value or "").strip()
    if not value:
        return None
    # Python's fromisoformat only accepts 'Z' from 3.11; normalise it.
    normalised = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_decimal(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _decode(raw_bytes):
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvImportError("Could not decode the file. Save it as UTF-8 and retry.")


def _sniff_delimiter(sample):
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
    except csv.Error:
        # The export is semicolon-delimited; fall back to that.
        return ";"


def import_sims(raw_bytes):
    """Import SIM rows from raw CSV bytes.

    Returns a dict: {created, updated, skipped, errors[]}.
    The whole import runs so one bad row doesn't abort the rest;
    call this inside a transaction if you want all-or-nothing.
    """
    text = _decode(raw_bytes)
    sample = text[:4096]
    delimiter = _sniff_delimiter(sample)

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames is None:
        raise CsvImportError("The file appears to be empty.")

    # Match known headers (trimmed); ignore unknown/empty columns.
    present = {h.strip(): h for h in reader.fieldnames if h}
    usable = {HEADER_MAP[h]: original for h, original in present.items() if h in HEADER_MAP}
    if "iccid" not in usable:
        raise CsvImportError(
            "No 'ICCID' column found. Expected the Transatel park export "
            f"(delimiter detected: '{delimiter}')."
        )

    created = updated = skipped = 0
    errors = []

    for line_no, row in enumerate(reader, start=2):  # row 1 is the header
        iccid = (row.get(usable["iccid"]) or "").strip()
        if not iccid:
            skipped += 1
            errors.append(f"Row {line_no}: missing ICCID, skipped.")
            continue

        fields = {}
        for field, header in usable.items():
            if field == "iccid":
                continue
            value = row.get(header)
            if field in DATE_FIELDS:
                fields[field] = _parse_datetime(value)
            elif field in DECIMAL_FIELDS:
                fields[field] = _parse_decimal(value)
            else:
                fields[field] = (value or "").strip()

        try:
            _, was_created = Sim.objects.update_or_create(iccid=iccid, defaults=fields)
        except Exception as exc:  # keep going on a single bad row
            skipped += 1
            errors.append(f"Row {line_no} (ICCID {iccid}): {exc}")
            continue

        if was_created:
            created += 1
        else:
            updated += 1

    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}
