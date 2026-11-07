"""Read-only lookups against the active roaming register."""

from datetime import date, timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from ..models import Destination, PlanTariffMap, RegisterEntry, RegisterVersion

# Using a phone in the UK (England, Scotland, Wales and Northern Ireland) is
# not roaming. Jersey, Guernsey and the Isle of Man have their own codes.
DOMESTIC_CODE = "GB"


def active_version() -> RegisterVersion | None:
    return RegisterVersion.objects.filter(status=RegisterVersion.Status.ACTIVE).first()


def plan_mapping(line) -> PlanTariffMap | None:
    """The SKU/version this line is billed under.

    Chosen by the line's own plan and term, as in effect on its activation
    date, so an older customer keeps the entitlement they signed up for.
    An exact term match beats a mapping that covers any term.
    """
    if not line.plan_id:
        return None
    anchor = (line.activated_at or line.created_at or timezone.now()).date()
    candidates = (
        PlanTariffMap.objects.filter(plan_id=line.plan_id, term__in=[line.term, ""], effective_from__lte=anchor)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=anchor))
        .order_by("-effective_from")
    )
    exact = [m for m in candidates if line.term and m.term == line.term]
    return (exact or list(candidates) or [None])[0]


def matching_entries(version: RegisterVersion, destination: Destination, travel_date: date, *,
                     plan_sku: str = "", plan_version: str = "") -> list[RegisterEntry]:
    """Every row for this plan (blank SKU = published rates) and destination on `travel_date`.

    The caller treats anything other than exactly one resolved row as
    "mapping pending": a missing row, an overlap or an unresolved row is never
    guessed around.
    """
    return list(
        RegisterEntry.objects.filter(
            version=version, destination=destination, plan_sku=plan_sku, plan_version=plan_version,
            effective_from__lte=travel_date,
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=travel_date))
    )


def single_resolved(entries: list[RegisterEntry]) -> RegisterEntry | None:
    if len(entries) == 1 and entries[0].row_status == RegisterEntry.RowStatus.RESOLVED:
        return entries[0]
    return None


def verified_on(entry: RegisterEntry) -> date:
    return entry.validation_date or entry.version.validation_date


def is_stale(entry: RegisterEntry) -> bool:
    max_age = timedelta(days=settings.ROAMING_REGISTER_STALE_DAYS)
    return timezone.localdate() - verified_on(entry) > max_age


def published_zones(version: RegisterVersion | None) -> dict[int, str]:
    """destination_id → rating zone from the published (blank-SKU) rows, for search results."""
    if version is None:
        return {}
    today = timezone.localdate()
    rows = (
        RegisterEntry.objects.filter(
            version=version, plan_sku="", row_status=RegisterEntry.RowStatus.RESOLVED,
            destination__isnull=False, effective_from__lte=today,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
        .values_list("destination_id", "rating_zone")
    )
    return dict(rows)
