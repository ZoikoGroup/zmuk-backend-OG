import csv
import io
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.utils import timezone

from apps.add_line.models import Line
from apps.plans.models import Plan
from apps.roaming.models import PlanTariffMap
from apps.roaming.services import importer

TODAY = date(2026, 11, 6)


def register_csv(rows: list[dict], delimiter: str = ",") -> bytes:
    """A register file with the full §04 header; unspecified columns are blank."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=importer.COLUMNS, delimiter=delimiter)
    writer.writeheader()
    for i, row in enumerate(rows, start=1):
        writer.writerow({
            "effective_from": "2026-01-01", "source_system": "billing", "source_record_id": f"R{i}",
            **row,
        })
    return out.getvalue().encode("utf-8")


def included_row(code: str, **extra) -> dict:
    return {
        "plan_sku": "STD10", "plan_version": "v1", "territory_code": code, "rating_zone": "Zone 2",
        "supplier_availability": "available", "inclusion": "included",
        "included_data_mb": "5120", "included_voice_minutes": "500", "included_sms": "500",
        "rate_data_per_mb": "0.0160", "rate_voice_to_uk_per_min": "0.065", "rate_sms": "0.10",
        "rate_units_rounding": "Data per KB", "vat_basis": "inclusive", "currency": "GBP",
        "exhaustion_action": "charge", "applicable_spend_control": "Customer-set bill limit applies", **extra,
    }


def charged_row(code: str, **extra) -> dict:
    return {
        "plan_sku": "STD10", "plan_version": "v1", "territory_code": code, "rating_zone": "Zone 3",
        "supplier_availability": "available", "inclusion": "not_included",
        "rate_data_per_mb": "0.1500", "rate_voice_to_uk_per_min": "0.250",
        "rate_voice_incoming_per_min": "0.150", "rate_sms": "0.10", "rate_units_rounding": "Data per KB",
        "vat_basis": "inclusive", "currency": "GBP", "exhaustion_action": "charge", **extra,
    }


def mapping_csv(rows: list[dict]) -> bytes:
    """A plan-mapping file with the agreed 7-column header."""
    from apps.roaming.services.plan_mapping import COLUMNS

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow({"term": "30D", "plan_sku": "STD10", "plan_version": "v1", "effective_from": "2025-01-01",
                         **row})
    return out.getvalue().encode("utf-8")


def load_register(rows: list[dict], *, label="test-r1", validation_date=None, activate=True):
    version = importer.import_register(
        register_csv(rows), label=label, validation_date=validation_date or timezone.localdate(),
    )
    return importer.activate(version) if activate else version


def make_plan(name="Standard 10GB") -> Plan:
    return Plan.objects.create(name=name, price=Decimal("10.00"), duration_days=30, transatelID="TSL_10")


def map_plan(plan, *, sku="STD10", version="v1", term="30D", effective_from=date(2025, 1, 1), effective_to=None):
    return PlanTariffMap.objects.create(
        plan=plan, term=term, plan_sku=sku, plan_version=version,
        effective_from=effective_from, effective_to=effective_to,
    )


def make_line(owner, plan=None, *, term="30D", status=Line.Status.ACTIVE, activated=date(2026, 3, 1)) -> Line:
    return Line.objects.create(
        owner=owner, plan=plan, term=term, status=status, current_msisdn="447700900123",
        activated_at=datetime(activated.year, activated.month, activated.day, tzinfo=dt_timezone.utc),
    )
