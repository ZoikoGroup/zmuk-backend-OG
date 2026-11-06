"""Customer-facing wording for roaming quotes (CT-001 §06–07, ROAM-005 §04).

Kept in the backend so the dashboard shows exactly the approved copy and
never writes its own.
"""

# Quote statuses (ROAM-005 §04). LINE_NOT_ACTIVE covers suspended, pending
# and terminated lines (CT-001 §09-6 safe states).
READY_PERSONALISED = "READY_PERSONALISED"
LEGACY_LINE_UNLINKED = "LEGACY_LINE_UNLINKED"
TARIFF_MAPPING_PENDING = "TARIFF_MAPPING_PENDING"
STALE = "STALE"
PUBLIC_RATES_ONLY = "PUBLIC_RATES_ONLY"
LINE_NOT_ACTIVE = "LINE_NOT_ACTIVE"

# Outcomes inside READY_PERSONALISED (CT-001 §06-3). No travel passes exist,
# so PASS_AVAILABLE is never returned (ROAM-005 §02 C).
INCLUDED_WITH_LIMIT = "INCLUDED_WITH_LIMIT"
STANDARD_CHARGES = "STANDARD_CHARGES"
NOT_SUPPORTED = "NOT_SUPPORTED"

_STATUS_COPY = {
    LEGACY_LINE_UNLINKED: (
        "We need to verify this number before we can show your personal roaming allowance. "
        "You can check our published roaming rates or contact support."
    ),
    TARIFF_MAPPING_PENDING: (
        "Your personal roaming charges are temporarily unavailable. "
        "Please check our published roaming rates or contact support before travelling."
    ),
    STALE: (
        "Your personal roaming charges are temporarily unavailable. "
        "Please check our published roaming rates or contact support before travelling."
    ),
    LINE_NOT_ACTIVE: "Choose an active mobile number to see your personal roaming allowances and charges.",
}

_ACTIONS = {
    INCLUDED_WITH_LIMIT: ("view_allowance", "View your roaming allowance"),
    STANDARD_CHARGES: ("view_rates", "View roaming rates"),
    NOT_SUPPORTED: ("contact_support", "Contact support"),
    PUBLIC_RATES_ONLY: ("view_rates", "View roaming rates"),
    LEGACY_LINE_UNLINKED: ("contact_support", "Contact support"),
    TARIFF_MAPPING_PENDING: ("contact_support", "Contact support"),
    STALE: ("contact_support", "Contact support"),
    LINE_NOT_ACTIVE: ("choose_line", "Choose an active number"),
}

PUBLIC_NOTICE = "Not a personalised quote"
UNVERIFIED_DESTINATION = (
    "We can't confirm roaming rates for this destination yet. Please contact support before travelling."
)
HIGH_RISK_WARNING = (
    "Ship and aircraft networks are charged separately and can be expensive, "
    "even near a country where roaming is included."
)


def status_message(status: str) -> str:
    return _STATUS_COPY[status]


def outcome_message(outcome: str, *, country: str, plan: str, allowance: str) -> str:
    if outcome == INCLUDED_WITH_LIMIT:
        return (
            f"Roaming is included in {country} with your {plan}, up to {allowance}. "
            "Charges may apply when your allowance runs out."
        )
    if outcome == STANDARD_CHARGES:
        return f"Roaming isn't included in your plan for {country}. Check rates before you use your phone."
    return f"Roaming isn't available in {country} on your plan. Please contact support before travelling."


def public_message(country: str) -> str:
    return f"{PUBLIC_NOTICE}. These are our published roaming rates for {country}."


def action(key: str) -> dict:
    kind, label = _ACTIONS[key]
    return {"kind": kind, "label": label}
