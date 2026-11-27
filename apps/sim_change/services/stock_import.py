"""Import Swap SIM stock from a CSV (CTO Addendum v2.1 B4).

The stock source (separate export, inventory feed or Auriga pool) is still to
be confirmed by Network Ops, so this takes a simple CSV and validates the
whole file first; nothing is saved unless every row is valid.

Columns:
    iccid            required, 18–22 digits
    sim_format       required, "esim" or "psim"
    supplier_status  optional, e.g. AVAILABLE
    supplier_pool_id optional (falls back to --pool)
    imsi, serial_number  optional
    msisdn           optional, but must be blank: Swap SIMs have no number

Rejected outright:
* an ICCID already held as a Sales SIM — never repurposed without explicit
  supplier certification;
* a Swap SIM that is reserved or bound to a line;
* a row whose format disagrees with the SIM we already hold.
"""

import csv
import io
import re
from dataclasses import dataclass, field

from django.db import transaction

from apps.sims.models import Sim

from ..models import SwapSimStock, current_tenant

REQUIRED_COLUMNS = ("iccid", "sim_format")
TYPE_OF_SIM = {"esim": "eUICC", "psim": "UICC"}
_ICCID = re.compile(r"^\d{18,22}$")


class StockImportError(Exception):
    pass


@dataclass
class Problem:
    row: int
    field: str
    rule: str


@dataclass
class ImportResult:
    row_count: int = 0
    created: int = 0
    updated: int = 0
    problems: list = field(default_factory=list)
    dry_run: bool = False


def _rows(raw: bytes):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StockImportError("The file must be UTF-8 CSV.") from exc
    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        raise StockImportError(f"Missing column(s): {', '.join(missing)}.")
    reader.fieldnames = headers
    for number, row in enumerate(reader, start=2):
        cleaned = {k: (v or "").strip() for k, v in row.items() if k}
        if any(cleaned.values()):
            yield number, cleaned


def _check_row(number, row, seen, problems):
    iccid = re.sub(r"\s", "", row.get("iccid", ""))
    fmt = row.get("sim_format", "").lower()
    before = len(problems)
    if not _ICCID.match(iccid):
        problems.append(Problem(number, "iccid", "must be 18–22 digits"))
    elif iccid in seen:
        problems.append(Problem(number, "iccid", f"duplicate of row {seen[iccid]}"))
    else:
        seen[iccid] = number
    if fmt not in TYPE_OF_SIM:
        problems.append(Problem(number, "sim_format", "must be esim or psim"))
    if row.get("msisdn"):
        problems.append(Problem(number, "msisdn", "Swap SIMs have no number; leave blank"))
    if len(problems) > before:
        return None

    existing = Sim.objects.filter(iccid=iccid).first()
    if existing is not None:
        if existing.inventory_purpose != Sim.Purpose.SWAP:
            problems.append(Problem(number, "iccid", "already held as a Sales SIM; can't become Swap stock"))
            return None
        if existing.sim_type_key != fmt:
            problems.append(Problem(number, "sim_format", f"we hold this SIM as {existing.sim_type_key}"))
            return None
        stock = getattr(existing, "swap_stock", None)
        if stock is not None and stock.status in (SwapSimStock.Status.RESERVED, SwapSimStock.Status.ASSIGNED):
            problems.append(Problem(number, "iccid", f"Swap SIM is {stock.status.lower()}; can't be re-imported"))
            return None
    return iccid, fmt, existing


def import_swap_stock(raw: bytes, *, source: str, pool: str = "", actor=None, dry_run=False) -> ImportResult:
    if not source.strip():
        raise StockImportError("Say where this stock came from (--source).")
    result = ImportResult(dry_run=dry_run)
    seen, checked = {}, []
    for number, row in _rows(raw):
        result.row_count += 1
        parsed = _check_row(number, row, seen, result.problems)
        if parsed:
            checked.append((row, *parsed))
    if not result.row_count:
        raise StockImportError("The file has a header but no data rows.")
    if result.problems or dry_run:
        result.created = sum(1 for *_, existing in checked if existing is None)
        result.updated = len(checked) - result.created
        return result

    tenant = current_tenant()
    with transaction.atomic():
        for row, iccid, fmt, existing in checked:
            supplier = {
                "supplier_status": row.get("supplier_status", "")[:32],
                "supplier_pool_id": (row.get("supplier_pool_id") or pool)[:64],
            }
            if existing is None:
                sim = Sim.objects.create(
                    iccid=iccid, imsi=row.get("imsi", "")[:32], serial_number=row.get("serial_number", "")[:64],
                    type_of_sim=TYPE_OF_SIM[fmt], inventory_purpose=Sim.Purpose.SWAP,
                    provisioning_status=supplier["supplier_status"],
                )
                SwapSimStock.objects.create(
                    tenant=tenant, sim=sim, sim_format=fmt, source=source.strip()[:128], imported_by=actor, **supplier,
                )
                result.created += 1
            else:
                SwapSimStock.objects.update_or_create(
                    sim=existing,
                    defaults={**supplier, "sim_format": fmt},
                    create_defaults={
                        **supplier, "tenant": tenant, "sim_format": fmt, "source": source.strip()[:128],
                        "imported_by": actor,
                    },
                )
                result.updated += 1
    return result
