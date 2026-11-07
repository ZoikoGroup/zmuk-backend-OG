"""Controls added for the Roaming Tariff Migration Engineering Decision v2 (6 Nov 2026)."""

import csv
import io
import tempfile
from datetime import date
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.roaming.models import Destination, PlanMappingImport, PlanTariffMap, RegisterEntry
from apps.roaming.services import plan_mapping, quote, reports

from .factories import (
    charged_row, included_row, load_register, make_line, make_plan, map_plan, mapping_csv,
)

TRAVEL = date(2026, 12, 1)


def statuses(version):
    return list(version.entries.order_by("row_number").values_list("row_status", flat=True))


class DestinationsMixin:
    @classmethod
    def setUpTestData(cls):
        call_command("load_roaming_destinations", stdout=io.StringIO())


# --- register import ----------------------------------------------------------

class RegisterValidationTests(DestinationsMixin, TestCase):
    def test_example_rows_are_rejected(self):  # A01
        version = load_register([
            included_row("FR", plan_sku="EXAMPLE-STD10"),
            included_row("ES", source_record_id="RT-1042"),
            charged_row("US", plan_sku="", plan_version="", source_record_id="WP-roaming-US"),
            included_row("IT"),
        ], activate=False)
        self.assertEqual(statuses(version), ["unresolved", "unresolved", "unresolved", "resolved"])
        self.assertIn("example/test row", version.entries.get(territory_code="FR").unresolved_reason)

    def test_currency_symbol_in_a_rate_is_rejected(self):
        version = load_register([included_row("FR", rate_sms="£0.10")], activate=False)
        entry = version.entries.get()
        self.assertEqual(entry.row_status, RegisterEntry.RowStatus.UNRESOLVED)
        self.assertIn("currency symbol", entry.unresolved_reason)

    def test_rates_are_never_rounded(self):
        version = load_register([
            included_row("FR", rate_data_per_mb="0.00125"),
            included_row("ES", rate_data_per_mb="3.125"),
            included_row("IT", rate_data_per_mb="NaN"),
        ], activate=False)
        self.assertEqual(statuses(version), ["unresolved", "resolved", "unresolved"])
        self.assertIn("more than 4 decimal places", version.entries.get(territory_code="FR").unresolved_reason)

    def test_blank_currency_and_vat_stay_unknown(self):
        version = load_register([included_row("FR", currency="", vat_basis="")], activate=False)
        entry = version.entries.get()
        self.assertEqual(entry.row_status, RegisterEntry.RowStatus.RESOLVED)
        self.assertEqual((entry.currency, entry.vat_basis), ("", ""))

    def test_invalid_vat_currency_and_duration_values(self):
        version = load_register([
            included_row("FR", vat_basis="incl"),
            included_row("ES", currency="pounds"),
            included_row("IT", contract_duration="1Y"),
            included_row("PT", vat_basis="Exclusive", currency="gbp", contract_duration="12m"),
        ], activate=False)
        self.assertEqual(statuses(version), ["unresolved", "unresolved", "unresolved", "resolved"])
        pt = version.entries.get(territory_code="PT")
        self.assertEqual((pt.vat_basis, pt.currency, pt.contract_duration), ("exclusive", "GBP", "12M"))

    def test_overlapping_windows_flag_both_rows(self):  # A09
        version = load_register([
            included_row("ES", effective_to="2026-06-30"),
            charged_row("ES", effective_from="2026-06-01"),
        ], activate=False)
        self.assertEqual(statuses(version), ["unresolved", "unresolved"])
        self.assertIn("overlaps row 3", version.entries.get(row_number=2).unresolved_reason)

    def test_adjacent_windows_and_other_terms_are_fine(self):
        version = load_register([
            included_row("ES", effective_to="2026-05-31"),
            charged_row("ES", effective_from="2026-06-01"),
            included_row("FR", contract_duration="30D"),
            charged_row("FR", contract_duration="12M"),
        ], activate=False)
        self.assertEqual(version.unresolved_count, 0)

    def test_problems_record_field_and_owner(self):
        version = load_register([included_row("ZZ", rate_sms="abc")], activate=False)
        problems = {p["field"]: p["owner"] for p in version.entries.get().problems}
        self.assertEqual(problems["territory_code"], "Transatel/Auriga Integration Ops")
        self.assertEqual(problems["rate_sms"], "Billing/Finance")


class RejectionReportTests(DestinationsMixin, TestCase):
    def report(self, version):
        out = io.StringIO()
        reports.write_report(out, reports.REGISTER_REPORT_COLUMNS, reports.register_report_rows(version))
        return list(csv.DictReader(io.StringIO(out.getvalue())))

    def test_one_line_per_problem_with_source_key(self):
        version = load_register([included_row("ZZ", rate_sms="abc", source_record_id="BILL-77"), included_row("FR")],
                                activate=False)
        rows = self.report(version)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["field"] for r in rows}, {"territory_code", "rate_sms"})
        self.assertEqual(rows[0]["row_number"], "2")
        self.assertEqual(rows[0]["source_record_id"], "BILL-77")

    def test_spreadsheet_formulas_are_neutralised(self):
        version = load_register([included_row("ZZ", source_record_id="=HYPERLINK(\"x\")")], activate=False)
        self.assertTrue(self.report(version)[0]["source_record_id"].startswith("'="))

    def test_admin_download_and_command_report(self):
        version = load_register([included_row("ZZ")], activate=False)
        admin = User.objects.create_superuser("ops", "ops@example.com", "pw")
        self.client.force_login(admin)
        resp = self.client.get(f"/admin/roaming/registerversion/{version.pk}/rejection-report/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("unknown territory_code 'ZZ'", resp.content.decode())
        self.assertEqual(self.client.get("/admin/roaming/registerversion/").status_code, 200)

        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / "r.csv", Path(tmp) / "report.csv"
            from .factories import register_csv
            src.write_bytes(register_csv([included_row("QQ")]))
            call_command("import_roaming_register", str(src), label="cmd", validation_date=date(2026, 11, 6),
                         report=str(out), stdout=io.StringIO())
            self.assertIn("unknown territory_code 'QQ'", out.read_text(encoding="utf-8"))


# --- quotes -------------------------------------------------------------------

class QuoteSafetyTests(DestinationsMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.plan = make_plan()

    def line_quote(self, code, term="30D"):
        line = make_line(self.user, self.plan, term=term)
        return quote.line_quote(self.user, line, Destination.objects.get(code=code), TRAVEL).data

    def test_missing_vat_currency_or_rounding_gives_no_money_quote(self):
        map_plan(self.plan)
        for field in ("vat_basis", "currency", "rate_units_rounding"):
            with self.subTest(field=field):
                load_register([included_row("FR", **{field: ""})], label=f"r-{field}")
                data = self.line_quote("FR")
                self.assertEqual(data["status"], "TARIFF_MAPPING_PENDING")
                self.assertIsNone(data["rates"])

    def test_public_rates_need_vat_basis_too(self):
        load_register([charged_row("US", plan_sku="", plan_version="", vat_basis="")])
        data = quote.public_quote(Destination.objects.get(code="US"), TRAVEL).data
        self.assertEqual(data["status"], "TARIFF_MAPPING_PENDING")

    def test_public_wording_says_plan_may_differ(self):
        load_register([charged_row("US", plan_sku="", plan_version="")])
        data = quote.public_quote(Destination.objects.get(code="US"), TRAVEL).data
        self.assertEqual(data["message"], "Not a personalised quote. These are our general published roaming "
                                          "rates for United States. Your plan may differ.")

    def test_contract_term_selects_its_own_row(self):  # A05
        map_plan(self.plan, term="")
        load_register([
            included_row("FR", contract_duration="30D"),
            charged_row("FR", contract_duration="12M"),
        ])
        self.assertEqual(self.line_quote("FR", term="30D")["outcome"], "INCLUDED_WITH_LIMIT")
        self.assertEqual(self.line_quote("FR", term="12M")["outcome"], "STANDARD_CHARGES")
        self.assertEqual(self.line_quote("FR", term="24M")["status"], "TARIFF_MAPPING_PENDING")

    def test_all_terms_row_used_only_without_a_term_row(self):
        map_plan(self.plan, term="")
        load_register([
            included_row("FR"),  # blank contract_duration = every term
            charged_row("FR", contract_duration="12M"),
        ])
        self.assertEqual(self.line_quote("FR", term="24M")["outcome"], "INCLUDED_WITH_LIMIT")
        self.assertEqual(self.line_quote("FR", term="12M")["outcome"], "STANDARD_CHARGES")


# --- plan mapping import ------------------------------------------------------

class PlanMappingImportTests(DestinationsMixin, TestCase):
    def setUp(self):
        self.plan = make_plan("Standard 10GB")
        load_register([included_row("FR")], label="r1", activate=False)  # a draft register is enough

    def run_import(self, rows, **kwargs):
        return plan_mapping.import_plan_mapping(mapping_csv(rows), file_name="map.csv", **kwargs)

    def test_valid_file_is_applied_as_a_batch(self):
        result = self.run_import([{"zoiko_plan_name": "Zoiko Standard 10GB"}])
        self.assertTrue(result.ok)
        mapping = PlanTariffMap.objects.get()
        self.assertEqual((mapping.plan, mapping.term, mapping.plan_sku, mapping.source_row),
                         (self.plan, "30D", "STD10", 2))
        self.assertEqual(mapping.import_batch, result.batch)
        self.assertEqual(result.batch.created_count, 1)

    def test_dry_run_saves_nothing(self):
        result = self.run_import([{"zoiko_plan_name": "Standard 10GB"}], dry_run=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.created, 1)
        self.assertFalse(PlanTariffMap.objects.exists() or PlanMappingImport.objects.exists())

    def test_one_bad_row_rejects_the_whole_file(self):
        result = self.run_import([
            {"zoiko_plan_name": "Standard 10GB"},
            {"zoiko_plan_name": "Galaxy 999GB"},
            {"zoiko_plan_name": "Standard 10GB", "term": "12M", "plan_sku": "EXAMPLE-STD10"},
            {"zoiko_plan_name": "Standard 10GB", "term": "24M", "plan_sku": "NOPE"},
            {"zoiko_plan_name": "Standard 10GB", "term": "6M", "effective_from": "01/01/2025"},
        ])
        self.assertFalse(result.ok)
        self.assertFalse(PlanTariffMap.objects.exists())
        found = {(p.row, p.field) for p in result.problems}
        self.assertTrue({(3, "zoiko_plan_name"), (4, "plan_sku"), (5, "plan_sku"), (6, "term"),
                         (6, "effective_from")} <= found)
        self.assertIn("import the register first", next(p.rule for p in result.problems if p.row == 5))

    def test_overlaps_within_file_and_with_existing_are_rejected(self):
        map_plan(self.plan, sku="STD10", version="v1", term="12M", effective_from=date(2025, 1, 1))
        result = self.run_import([
            {"zoiko_plan_name": "Standard 10GB", "effective_to": "2025-12-31"},
            {"zoiko_plan_name": "Standard 10GB", "effective_from": "2025-06-01"},
            {"zoiko_plan_name": "Standard 10GB", "term": "12M", "effective_from": "2026-01-01"},
        ])
        self.assertEqual({p.row for p in result.problems}, {3, 4})

    def test_reupload_of_identical_rows_is_skipped(self):
        self.run_import([{"zoiko_plan_name": "Standard 10GB"}])
        again = self.run_import([{"zoiko_plan_name": "Standard 10GB", "notes": "re-sent"}])
        self.assertTrue(again.ok)
        self.assertEqual((again.created, again.skipped), (0, 1))
        self.assertEqual(PlanTariffMap.objects.count(), 1)

    def test_same_file_twice_is_refused(self):
        raw = mapping_csv([{"zoiko_plan_name": "Standard 10GB"}])
        plan_mapping.import_plan_mapping(raw)
        with self.assertRaisesMessage(plan_mapping.PlanMappingImportError, "already imported"):
            plan_mapping.import_plan_mapping(raw)

    def test_deleting_the_batch_rolls_back_its_mappings(self):
        manual = map_plan(self.plan, term="24M")
        result = self.run_import([{"zoiko_plan_name": "Standard 10GB"}])
        result.batch.delete()
        self.assertEqual(list(PlanTariffMap.objects.all()), [manual])

    def test_imported_mapping_drives_the_quote(self):
        PlanTariffMap.objects.all().delete()
        from apps.roaming.models import RegisterVersion
        from apps.roaming.services import importer
        importer.activate(RegisterVersion.objects.get(label="r1"))
        self.run_import([{"zoiko_plan_name": "Standard 10GB"}])
        user = User.objects.create_user("alice", "alice@example.com", "pw")
        data = quote.line_quote(user, make_line(user, self.plan), Destination.objects.get(code="FR"), TRAVEL).data
        self.assertEqual(data["outcome"], "INCLUDED_WITH_LIMIT")

    def test_management_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, bad, report = Path(tmp) / "good.csv", Path(tmp) / "bad.csv", Path(tmp) / "problems.csv"
            good.write_bytes(mapping_csv([{"zoiko_plan_name": "Standard 10GB"}]))
            bad.write_bytes(mapping_csv([{"zoiko_plan_name": "Nope"}]))

            out = io.StringIO()
            call_command("import_plan_tariff_map", str(good), dry_run=True, stdout=out)
            self.assertIn("Would create 1", out.getvalue())
            self.assertFalse(PlanTariffMap.objects.exists())

            with self.assertRaisesMessage(CommandError, "Not applied"):
                call_command("import_plan_tariff_map", str(bad), report=str(report), stdout=io.StringIO())
            self.assertIn("no plan called 'Nope'", report.read_text(encoding="utf-8"))

            call_command("import_plan_tariff_map", str(good), stdout=io.StringIO())
            self.assertEqual(PlanTariffMap.objects.count(), 1)

    def test_admin_upload(self):
        admin = User.objects.create_superuser("ops", "ops@example.com", "pw")
        self.client.force_login(admin)
        url = "/admin/roaming/planmappingimport/import-csv/"
        self.assertEqual(self.client.get(url).status_code, 200)

        def upload(rows, dry_run):
            f = io.BytesIO(mapping_csv(rows))
            f.name = "map.csv"
            data = {"csv_file": f, "notes": ""}
            if dry_run:
                data["dry_run"] = "on"
            return self.client.post(url, data)

        resp = upload([{"zoiko_plan_name": "Nope"}], dry_run=False)
        self.assertContains(resp, "no plan called")
        self.assertFalse(PlanTariffMap.objects.exists())

        resp = upload([{"zoiko_plan_name": "Standard 10GB"}], dry_run=True)
        self.assertContains(resp, "would be created")
        self.assertFalse(PlanTariffMap.objects.exists())

        resp = upload([{"zoiko_plan_name": "Standard 10GB"}], dry_run=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(PlanTariffMap.objects.count(), 1)
        self.assertEqual(self.client.get("/admin/roaming/planmappingimport/").status_code, 200)
