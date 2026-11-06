"""The roaming quote decision (CT-001 §06/§08, ROAM-005 §04).

Strictly read-only: nothing here charges, bars, activates or calls Transatel.
Every uncertain case falls back to a "check published rates / contact
support" status; there is no silent free-roaming fallback and an unknown
figure is null, never 0.

    line not ACTIVE                         → LINE_NOT_ACTIVE
    line has no plan (unverified legacy)    → LEGACY_LINE_UNLINKED
    no active register / no plan mapping /
      missing, overlapping or unresolved row → TARIFF_MAPPING_PENDING
    row older than ROAMING_REGISTER_STALE_DAYS → STALE
    otherwise                               → READY_PERSONALISED with outcome
        supplier_availability not_available → NOT_SUPPORTED
        inclusion included                  → INCLUDED_WITH_LIMIT
        inclusion not_included + a known rate → STANDARD_CHARGES
        anything else unknown               → TARIFF_MAPPING_PENDING
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.utils import timezone

from apps.add_line.models import Line

from ..models import Destination, RegisterEntry, RoamingQuote
from . import balance, messages, register
from .register import DOMESTIC_CODE

# (response key, model field, charging unit) for each rate tile the dashboard shows.
RATE_TILES = (
    ("data", "rate_data_per_mb", "MB"),
    ("voice_to_uk", "rate_voice_to_uk_per_min", "minute"),
    ("voice_local", "rate_voice_local_per_min", "minute"),
    ("voice_incoming", "rate_voice_incoming_per_min", "minute"),
    ("sms", "rate_sms", "text"),
    ("mms", "rate_mms", "picture message"),
)

_CURRENCY_SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$"}


class QuoteError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class QuoteResult:
    data: dict
    quote: RoamingQuote | None = None


def _money(amount: Decimal, currency: str) -> str:
    symbol = _CURRENCY_SYMBOLS.get(currency)
    return f"{symbol}{amount}" if symbol else f"{amount} {currency}"


def _rates(entry: RegisterEntry) -> dict:
    tiles = {}
    for key, field, unit in RATE_TILES:
        amount = getattr(entry, field)
        tiles[key] = None if amount is None else {
            "amount": str(amount),
            "unit": unit,
            "display": f"{_money(amount, entry.currency)} per {unit}",
        }
    return tiles


def _has_any_rate(entry: RegisterEntry) -> bool:
    return any(getattr(entry, field) is not None for _, field, _ in RATE_TILES)


def _charging(entry: RegisterEntry) -> dict:
    return {
        "currency": entry.currency,
        "vat_basis": entry.vat_basis or None,
        "units_rounding": entry.rate_units_rounding or None,
        "exhaustion_action": entry.exhaustion_action,
        "spend_control": entry.applicable_spend_control or None,
    }


def _data_text(mb: int) -> str:
    if mb < 1024:
        return f"{mb} MB"
    gb = Decimal(mb) / 1024
    return f"{gb.quantize(Decimal('0.1')).normalize():f} GB"


def _allowance_text(entry: RegisterEntry) -> str:
    parts = []
    if entry.included_data_mb is not None:
        parts.append(f"{_data_text(entry.included_data_mb)} of data")
    if entry.voice_unlimited:
        parts.append("unlimited minutes")
    elif entry.included_voice_minutes is not None:
        parts.append(f"{entry.included_voice_minutes} minutes")
    if entry.sms_unlimited:
        parts.append("unlimited texts")
    elif entry.included_sms is not None:
        parts.append(f"{entry.included_sms} texts")
    if not parts:
        return "your plan's roaming allowance"
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def _entitlement(entry: RegisterEntry, line) -> dict:
    return {
        "data_mb": entry.included_data_mb,
        "voice_minutes": entry.included_voice_minutes,
        "voice_unlimited": entry.voice_unlimited,
        "sms": entry.included_sms,
        "sms_unlimited": entry.sms_unlimited,
        "fair_use_terms": entry.fair_use_terms or None,
        "remaining": balance.remaining_allowance(line),
    }


def _commercial_basis(entry: RegisterEntry) -> dict:
    return {
        "register_version": entry.version.label,
        "plan_sku": entry.plan_sku or None,
        "plan_version": entry.plan_version or None,
        "effective_from": entry.effective_from,
        "effective_to": entry.effective_to,
        "validation_date": register.verified_on(entry),
        "source_system": entry.source_system,
    }


def _destination(destination: Destination, entry: RegisterEntry | None) -> dict:
    return {
        "code": destination.code,
        "name": destination.name,
        "kind": destination.kind,
        "rating_zone": (entry.rating_zone or None) if entry else None,
    }


def _warnings(destination: Destination) -> list[dict]:
    if destination.is_high_risk:
        return [{"code": "high_risk_network", "message": messages.HIGH_RISK_WARNING}]
    return []


def _masked(msisdn: str) -> str | None:
    digits = "".join(ch for ch in msisdn if ch.isdigit())
    if len(digits) < 4:
        return None
    return "•" * (len(digits) - 3) + digits[-3:]


def get_destination(code: str) -> Destination:
    code = (code or "").strip().upper()
    if code == DOMESTIC_CODE:
        raise QuoteError("DOMESTIC_DESTINATION", "Using your phone in the UK isn't roaming.")
    destination = Destination.objects.filter(code=code, is_active=True).first()
    if destination is None:
        raise QuoteError("UNKNOWN_DESTINATION", "We don't recognise that destination.")
    return destination


def line_quote(user, line: Line, destination: Destination, travel_date: date) -> QuoteResult:
    """Personalised quote for one of the customer's own lines; always saved for audit."""
    version = register.active_version()
    entry = None
    status, outcome = messages.READY_PERSONALISED, ""

    if line.status != Line.Status.ACTIVE:
        status = messages.LINE_NOT_ACTIVE
    elif not line.plan_id:
        status = messages.LEGACY_LINE_UNLINKED
    else:
        mapping = register.plan_mapping(line)
        if version is None or mapping is None:
            status = messages.TARIFF_MAPPING_PENDING
        else:
            entry = register.single_resolved(register.matching_entries(
                version, destination, travel_date, plan_sku=mapping.plan_sku, plan_version=mapping.plan_version,
            ))
            if entry is None:
                status = messages.TARIFF_MAPPING_PENDING
            elif register.is_stale(entry):
                status = messages.STALE
            elif entry.supplier_availability == RegisterEntry.Availability.NOT_AVAILABLE:
                outcome = messages.NOT_SUPPORTED
            elif entry.supplier_availability == RegisterEntry.Availability.UNKNOWN:
                status = messages.TARIFF_MAPPING_PENDING
            elif entry.inclusion == RegisterEntry.Inclusion.INCLUDED:
                outcome = messages.INCLUDED_WITH_LIMIT
            elif entry.inclusion == RegisterEntry.Inclusion.NOT_INCLUDED and _has_any_rate(entry):
                outcome = messages.STANDARD_CHARGES
            else:
                status = messages.TARIFF_MAPPING_PENDING

    plan_name = line.plan.name if line.plan_id else None
    data = {
        "status": status,
        "outcome": outcome or None,
        "line": {
            "id": str(line.uuid),
            "msisdn_masked": _masked(line.current_msisdn),
            "plan": plan_name,
            "status": line.status,
        },
        "destination": _destination(destination, entry if outcome else None),
        "travel_date": travel_date,
        "warnings": _warnings(destination),
        "generated_at": timezone.now(),
    }
    if outcome:
        data.update({
            "message": messages.outcome_message(
                outcome, country=destination.name, plan=plan_name, allowance=_allowance_text(entry),
            ),
            "action": messages.action(outcome),
            "commercial_basis": _commercial_basis(entry),
            "entitlement": _entitlement(entry, line) if outcome == messages.INCLUDED_WITH_LIMIT else None,
            "rates": _rates(entry),
            "charging": _charging(entry),
        })
    else:
        data.update({
            "message": messages.status_message(status),
            "action": messages.action(status),
            "commercial_basis": None,
            "entitlement": None,
            "rates": None,
            "charging": None,
        })

    quote = RoamingQuote.objects.create(
        user=user, line=line, destination=destination, travel_date=travel_date,
        register_version=version, entry=entry if outcome else None,
        status=status, outcome=outcome, response=_jsonable(data),
    )
    data["quote_id"] = str(quote.uuid)
    return QuoteResult(data=data, quote=quote)


def public_quote(destination: Destination, travel_date: date) -> QuoteResult:
    """Published general rates for a destination: no line, nothing personal."""
    version = register.active_version()
    entry = None
    if version is not None:
        entry = register.single_resolved(register.matching_entries(version, destination, travel_date))
    if entry is not None and (register.is_stale(entry) or not _has_any_rate(entry)):
        entry = None

    data = {
        "status": messages.PUBLIC_RATES_ONLY if entry else messages.TARIFF_MAPPING_PENDING,
        "outcome": None,
        "personalised": False,
        "destination": _destination(destination, entry),
        "travel_date": travel_date,
        "warnings": _warnings(destination),
        "generated_at": timezone.now(),
    }
    if entry:
        data.update({
            "message": messages.public_message(destination.name),
            "action": messages.action(messages.PUBLIC_RATES_ONLY),
            "commercial_basis": _commercial_basis(entry),
            "rates": _rates(entry),
            "charging": _charging(entry),
        })
    else:
        data.update({
            "message": messages.UNVERIFIED_DESTINATION,
            "action": messages.action(messages.TARIFF_MAPPING_PENDING),
            "commercial_basis": None,
            "rates": None,
            "charging": None,
        })
    return QuoteResult(data=data)


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, (date,)):
        return value.isoformat()
    return value
