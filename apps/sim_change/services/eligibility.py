"""Can this line change SIM type, and how risky is the request?

Eligibility (CTO v2.0 Q6, Addendum v2.1 B5) — a request is blocked when:
* the journey is switched off (`SIM_TYPE_CHANGE_MODE="off"`);
* the line isn't the customer's, isn't ACTIVE or has no SIM;
* the supplier read failed, or shows the SIM not active or a different
  number — stale or unknown supplier status is never actionable;
* the plan has no enabled eligibility rule for this direction (unknown plans
  are not eligible);
* the line was claimed from a legacy account and that is switched off;
* another change, a number transfer or an open SIM type change is in progress;
* the account is under security review (fraud hold or a recent report).

Risk (v2.1 A3 / B3) — never a block on its own, but raises the risk tier so
the agent sees it and a second approver is required:
* recent password, email, phone or address changes;
* a legacy-claimed line; repeated requests.
Unknown history before tracking began is shown to the agent
(`UNKNOWN_PRE_TRACKING`), not held automatically.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.add_line.models import Line, SimType
from apps.security.models import AccountSecurityEvent, SuspiciousReport
from apps.security.services import recent_changes

from ..models import Direction, SimChangePlanEligibility, SimTypeChangeCase, current_tenant
from . import line_lock, stock, supplier

K = AccountSecurityEvent.Kind

RISK_FOR_CHANGE = {
    K.PASSWORD_CHANGED: "RECENT_PASSWORD_CHANGE",
    K.EMAIL_CHANGED: "RECENT_EMAIL_CHANGE",
    K.PHONE_CHANGED: "RECENT_PHONE_CHANGE",
    K.DELIVERY_ADDRESS_CHANGED: "RECENT_ADDRESS_CHANGE",
    K.BILLING_ADDRESS_CHANGED: "RECENT_ADDRESS_CHANGE",
}
HIGH_RISK_FLAGS = set(RISK_FOR_CHANGE.values()) | {"LEGACY_CLAIMED_LINE", "REPEATED_REQUESTS"}
REPEAT_WINDOW = timedelta(days=30)
REPEAT_LIMIT = 3
REVIEW_WINDOW = timedelta(days=30)

MESSAGES = {
    "FEATURE_DISABLED": "Changing your SIM type isn't available right now.",
    "LINE_NOT_ACTIVE": "This number isn't active, so its SIM type can't be changed.",
    "NO_SIM_ON_LINE": "We can't find a SIM on this number.",
    "SUPPLIER_STATUS_UNAVAILABLE": "We can't check your SIM with our network partner right now. Please try again later or contact support.",
    "SUPPLIER_NOT_ACTIVE": "Our network partner doesn't show this SIM as active. Please contact support.",
    "SUPPLIER_MISMATCH": "We need to check this number's details before it can be changed. Please contact support.",
    "PLAN_NOT_ELIGIBLE": "Your plan doesn't support changing SIM type yet.",
    "LEGACY_LINE_REVIEW": "This number needs an extra check before its SIM type can be changed. Please contact support.",
    "OPEN_REQUEST": "You already have a SIM type change in progress for this number.",
    "LINE_OPERATION_IN_PROGRESS": "Another change is already in progress on this number.",
    "PORT_IN_PROGRESS": "A number transfer is in progress on this number.",
    "ACCOUNT_UNDER_REVIEW": "We've paused changes to protect your account. Please contact our security support team.",
}


@dataclass
class Assessment:
    line: Line
    current_type: str = ""
    target_type: str = ""
    direction: str = ""
    blocked: list = field(default_factory=list)   # [code, ...]
    risk_flags: list = field(default_factory=list)
    history_status: str = ""
    read: supplier.LineRead | None = None
    open_case: SimTypeChangeCase | None = None
    plan_rule: SimChangePlanEligibility | None = None
    stock_available: bool | None = None

    @property
    def eligible(self) -> bool:
        return not self.blocked

    @property
    def risk_tier(self) -> str:
        return "high" if HIGH_RISK_FLAGS & set(self.risk_flags) else "standard"

    @property
    def supplier_unavailable(self) -> bool:
        return "SUPPLIER_STATUS_UNAVAILABLE" in self.blocked


def mode() -> str:
    return getattr(settings, "SIM_TYPE_CHANGE_MODE", "assisted")


def plan_rule(line) -> SimChangePlanEligibility | None:
    if not line.plan_id:
        return None
    return SimChangePlanEligibility.objects.filter(
        tenant=current_tenant(), plan_id=line.plan_id, enabled=True, effective_from__lte=timezone.now(),
    ).order_by("-effective_from").first()


def account_under_review(user) -> bool:
    if SimTypeChangeCase.objects.filter(owner=user, status=SimTypeChangeCase.Status.HOLD_FRAUD).exists():
        return True
    return SuspiciousReport.objects.filter(user=user, created_at__gte=timezone.now() - REVIEW_WINDOW).exists()


def risk_flags(user, line) -> tuple[list, str]:
    recent = recent_changes(user, int(getattr(settings, "SIM_CHANGE_RISK_REVIEW_DAYS", 14)))
    flags = sorted({RISK_FOR_CHANGE[k] for k in recent.kinds if k in RISK_FOR_CHANGE})
    if line.source == Line.Source.LEGACY_CLAIM:
        flags.append("LEGACY_CLAIMED_LINE")
    since = timezone.now() - REPEAT_WINDOW
    if SimTypeChangeCase.objects.filter(owner=user, created_at__gte=since).count() >= REPEAT_LIMIT:
        flags.append("REPEATED_REQUESTS")
    if not recent.history_complete:
        flags.append("HISTORY_UNKNOWN_PRE_TRACKING")
    return flags, recent.history_status


def assess(user, line, *, to_type: str | None = None, read_supplier: bool = True) -> Assessment:
    """Eligibility and risk for `line`. `to_type` defaults to the opposite type."""
    result = Assessment(line=line)
    if mode() == "off":
        result.blocked.append("FEATURE_DISABLED")
        return result
    if line.sim_id is None:
        result.blocked.append("NO_SIM_ON_LINE")
        return result

    result.current_type = line.sim.sim_type_key
    result.target_type = to_type or (SimType.ESIM if result.current_type == SimType.PSIM else SimType.PSIM)
    if result.target_type != result.current_type:
        result.direction = Direction.for_types(result.current_type, result.target_type)

    if line.status != Line.Status.ACTIVE:
        result.blocked.append("LINE_NOT_ACTIVE")

    result.open_case = SimTypeChangeCase.objects.filter(line=line).exclude(
        status__in=SimTypeChangeCase.TERMINAL_STATUSES
    ).first()
    if result.open_case is not None:
        result.blocked.append("OPEN_REQUEST")
    else:
        held = line_lock.current_lock(line)
        if held is not None:
            result.blocked.append("LINE_OPERATION_IN_PROGRESS")
        elif line_lock.open_port_request(line) is not None:
            result.blocked.append("PORT_IN_PROGRESS")

    if account_under_review(user):
        result.blocked.append("ACCOUNT_UNDER_REVIEW")

    if line.source == Line.Source.LEGACY_CLAIM and not getattr(settings, "SIM_CHANGE_LEGACY_CLAIMS_ENABLED", False):
        result.blocked.append("LEGACY_LINE_REVIEW")

    result.plan_rule = plan_rule(line)
    if not result.direction or result.plan_rule is None or not result.plan_rule.allows(result.direction):
        result.blocked.append("PLAN_NOT_ELIGIBLE")

    if read_supplier and line.status == Line.Status.ACTIVE:
        result.read = supplier.read_line(line)
        if not result.read.ok:
            result.blocked.append("SUPPLIER_STATUS_UNAVAILABLE")
        elif not result.read.active:
            result.blocked.append("SUPPLIER_NOT_ACTIVE")
        elif not result.read.msisdn_matches:
            result.blocked.append("SUPPLIER_MISMATCH")

    result.risk_flags, result.history_status = risk_flags(user, line)
    if result.direction:
        probe = SimTypeChangeCase(tenant=current_tenant(), to_type=result.target_type)
        result.stock_available = stock.available_count(probe) > 0
    return result


def first_block(result: Assessment) -> tuple[str, str]:
    code = result.blocked[0]
    return code, MESSAGES.get(code, "This change isn't available.")
