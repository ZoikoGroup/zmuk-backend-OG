"""Compatibility helpers for the Stripe SDK.

stripe>=15 objects are no longer dictionaries (no .get()), so code written as
``obj.get("metadata")`` raises AttributeError. Convert to plain dicts first.
"""


def to_plain(obj):
    """Fully plain nested dicts/lists for a Stripe object; anything else is returned unchanged."""
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return obj
