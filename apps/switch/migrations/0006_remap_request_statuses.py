"""Map the Phase 1 statuses onto the CTO Milestone 04 request state machine.

    pending       → validating, or action_needed (code_needed) if a code is
                    still missing — the same rule new requests follow
    under_review  → validating
    processing    → ready_for_order
    completed     → converted (legacy, handled outside the system; noted)
    failed        → rejected
    cancelled     → cancelled

History rows are rewritten with the same map. Requests moved to
action_needed also get a history entry, since that is a real change.
"""

from django.db import migrations

STATUS_MAP = {
    "pending": "validating",
    "under_review": "validating",
    "processing": "ready_for_order",
    "completed": "converted",
    "failed": "rejected",
    "cancelled": "cancelled",
}
NEEDS_CODE = {"keep_pac", "new_stac"}
NOTE = "Status model updated to CTO Milestone 04 (migration 0006)"


def forwards(apps, schema_editor):
    SwitchRequest = apps.get_model("switch", "SwitchRequest")
    SwitchStatusEvent = apps.get_model("switch", "SwitchStatusEvent")

    for event in SwitchStatusEvent.objects.all():
        before = STATUS_MAP.get(event.status_before, event.status_before)
        after = STATUS_MAP.get(event.status_after, event.status_after)
        if (before, after) != (event.status_before, event.status_after):
            event.status_before, event.status_after = before, after
            event.save(update_fields=["status_before", "status_after"])

    for row in SwitchRequest.objects.all():
        old = row.status
        new = STATUS_MAP.get(old, old)
        reason = ""
        if old == "pending" and row.switch_route in NEEDS_CODE and not row.encrypted_code:
            new, reason = "action_needed", "code_needed"
        if new == old:
            continue
        row.status, row.action_reason = new, reason
        fields = ["status", "action_reason"]
        if old == "completed":
            row.ops_note = (row.ops_note + "\n" if row.ops_note else "") + (
                "Legacy: completed outside the system before Milestone 4; no order is linked."
            )
            fields.append("ops_note")
        row.save(update_fields=fields)
        if reason:
            # A real change beyond the rename: tell the customer a code is needed.
            SwitchStatusEvent.objects.create(
                switch_request=row, kind="status_changed", status_before=STATUS_MAP[old],
                status_after=new, action_reason=reason, note=NOTE,
            )


class Migration(migrations.Migration):

    dependencies = [
        ("switch", "0005_m04_request_state_machine"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
