import io
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.roaming.models import RegisterEntry, RegisterVersion
from apps.roaming.services import importer

from .factories import TODAY, charged_row, included_row, load_register, register_csv


class RegisterImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_roaming_destinations", stdout=io.StringIO())

    def test_import_creates_draft_with_all_rows(self):
        version = load_register([included_row("FR"), charged_row("CH")], activate=False)
        self.assertEqual(version.status, RegisterVersion.Status.DRAFT)
        self.assertEqual((version.row_count, version.unresolved_count), (2, 0))
        fr = version.entries.get(territory_code="FR")
        self.assertEqual(fr.destination.code, "FR")
        self.assertEqual(fr.rate_data_per_mb, Decimal("0.0160"))
        self.assertEqual(fr.included_data_mb, 5120)

    def test_bad_rows_are_kept_as_unresolved_with_reason(self):
        version = load_register([
            included_row("ZZ"),
            included_row("ES", rate_sms="ten pence"),
            included_row("IT", effective_from=""),
            included_row("PT", source_record_id=""),
        ], activate=False)
        self.assertEqual((version.row_count, version.unresolved_count), (4, 4))
        reasons = {e.territory_code: e.unresolved_reason for e in version.entries.all()}
        self.assertIn("unknown territory_code 'ZZ'", reasons["ZZ"])
        self.assertIn("rate_sms 'ten pence' is not a number", reasons["ES"])
        self.assertIn("effective_from is blank", reasons["IT"])
        self.assertIn("data lineage", reasons["PT"])
        self.assertIsNone(version.entries.get(territory_code="ES").rate_sms)

    def test_unknown_choice_value_is_unresolved(self):
        version = load_register([included_row("FR", inclusion="free")], activate=False)
        entry = version.entries.get()
        self.assertEqual(entry.row_status, RegisterEntry.RowStatus.UNRESOLVED)
        self.assertEqual(entry.inclusion, RegisterEntry.Inclusion.UNKNOWN)

    def test_duplicate_row_is_unresolved(self):
        version = load_register([included_row("FR"), included_row("FR")], activate=False)
        statuses = list(version.entries.values_list("row_status", flat=True))
        self.assertEqual(statuses, ["resolved", "unresolved"])

    def test_unlimited_voice_and_sms(self):
        version = load_register(
            [included_row("FR", included_voice_minutes="Unlimited", included_sms="unlimited")], activate=False
        )
        entry = version.entries.get()
        self.assertTrue(entry.voice_unlimited and entry.sms_unlimited)
        self.assertIsNone(entry.included_voice_minutes)

    def test_semicolon_delimited_file(self):
        raw = register_csv([included_row("FR")], delimiter=";")
        version = importer.import_register(raw, label="semi", validation_date=TODAY)
        self.assertEqual(version.unresolved_count, 0)

    def test_same_file_cannot_be_imported_twice(self):
        raw = register_csv([included_row("FR")])
        importer.import_register(raw, label="first", validation_date=TODAY)
        with self.assertRaisesMessage(importer.RegisterImportError, "already imported as 'first'"):
            importer.import_register(raw, label="second", validation_date=TODAY)

    def test_missing_required_column_rejects_whole_file(self):
        with self.assertRaisesMessage(importer.RegisterImportError, "territory_code"):
            importer.import_register(b"plan_sku,effective_from\nX,2026-01-01\n", label="x", validation_date=TODAY)
        self.assertFalse(RegisterVersion.objects.exists())

    def test_activate_retires_previous_version(self):
        first = load_register([included_row("FR")], label="r1")
        second = load_register([included_row("ES")], label="r2")
        first.refresh_from_db()
        self.assertEqual(first.status, RegisterVersion.Status.RETIRED)
        self.assertEqual(second.status, RegisterVersion.Status.ACTIVE)

    def test_management_command_refuses_to_activate_with_unresolved_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "register.csv"
            path.write_bytes(register_csv([included_row("ZZ")]))
            with self.assertRaisesMessage(CommandError, "Not activated"):
                call_command("import_roaming_register", str(path), label="cmd", validation_date=date(2026, 11, 1),
                             activate=True, stdout=io.StringIO())
        self.assertEqual(RegisterVersion.objects.get().status, RegisterVersion.Status.DRAFT)
