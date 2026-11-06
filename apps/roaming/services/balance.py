"""Remaining roaming allowance for a line.

The balance will come from the usage platform another team is building (not
BeQuick, not Transatel directly). Until that exists every line reports
"coming_soon" with null figures: an unknown balance is never shown as 0.
Connect the platform here; nothing else in the quote needs to change.
"""


def remaining_allowance(line) -> dict:
    return {
        "status": "coming_soon",
        "data_mb": None,
        "voice_minutes": None,
        "sms": None,
        "as_of": None,
    }
