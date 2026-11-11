"""Backfill for Switch & Save v2 (separate migration so the data writes and
the later ALTER TABLE don't share a transaction on PostgreSQL)."""

import secrets

from django.db import migrations

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _reference(taken):
    while True:
        ref = "ZM-SW-" + "".join(secrets.choice(_ALPHABET) for _ in range(8))
        if ref not in taken:
            taken.add(ref)
            return ref


def backfill(apps, schema_editor):
    """Give existing rows a unique reference and a history entry, map the old
    "Other" provider, and move any plain-text PAC/STAC into encrypted storage
    (decision C13) before the plain columns are dropped."""
    SwitchRequest = apps.get_model("switch", "SwitchRequest")
    SwitchStatusEvent = apps.get_model("switch", "SwitchStatusEvent")

    rows = list(SwitchRequest.objects.all())
    if any(r.pac_code or r.stac_code for r in rows):
        from apps.add_line.services import pac  # needs PAC_ENCRYPTION_KEY
        try:
            pac.encrypt("probe")
        except pac.PacStorageUnavailable as exc:
            raise RuntimeError(
                "Existing switch requests hold plain-text PAC/STAC codes. Set "
                "PAC_ENCRYPTION_KEY before running this migration so they can be "
                "encrypted instead of dropped."
            ) from exc
    else:
        pac = None

    taken = set()
    for row in rows:
        row.reference = _reference(taken)
        if row.current_provider == "Other":
            row.current_provider = "Other UK Provider"
        if pac and (row.pac_code or row.stac_code):
            code_type = "PAC" if row.pac_code else "STAC"
            row.encrypted_code = pac.encrypt((row.pac_code or row.stac_code).strip().upper())
            row.code_type = code_type
            row.switch_route = "keep_pac" if code_type == "PAC" else "new_stac"
            row.code_received_at = row.updated_at
        row.save()
        SwitchStatusEvent.objects.create(
            switch_request=row, kind="created", status_after=row.status,
            note="Backfilled by migration 0002",
        )


class Migration(migrations.Migration):

    dependencies = [
        ("switch", "0002_switch_and_save_v2"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
