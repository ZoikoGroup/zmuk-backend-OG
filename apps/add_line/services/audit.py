from ..models import LineEvent


def record(kind, *, line=None, order=None, port_request=None, actor=None,
           before="", after="", **detail):
    """Append one row to the add-line audit trail."""
    return LineEvent.objects.create(
        kind=kind, line=line, order=order, port_request=port_request, actor=actor,
        status_before=before or "", status_after=after or "", detail=detail,
    )
