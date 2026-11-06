import io
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.add_line.models import Line
from apps.roaming.models import Destination, RoamingQuote
from apps.roaming.services import quote

from .factories import charged_row, included_row, load_register, make_line, make_plan, map_plan

TRAVEL = date(2026, 12, 1)


def dest(code):
    return Destination.objects.get(code=code)


@override_settings(ROAMING_REGISTER_STALE_DAYS=90)
class LineQuoteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_roaming_destinations", stdout=io.StringIO())

    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.plan = make_plan()
        map_plan(self.plan)
        self.line = make_line(self.user, self.plan)

    def quote_for(self, code, line=None, travel=TRAVEL):
        return quote.line_quote(self.user, line or self.line, dest(code), travel).data

    # --- outcomes -------------------------------------------------------------

    def test_included_destination(self):
        load_register([included_row("FR")])
        data = self.quote_for("FR")
        self.assertEqual((data["status"], data["outcome"]), ("READY_PERSONALISED", "INCLUDED_WITH_LIMIT"))
        self.assertEqual(
            data["message"],
            "Roaming is included in France with your Standard 10GB, up to 5 GB of data, 500 minutes and "
            "500 texts. Charges may apply when your allowance runs out.",
        )
        self.assertEqual(data["action"], {"kind": "view_allowance", "label": "View your roaming allowance"})
        self.assertEqual(data["entitlement"]["data_mb"], 5120)
        self.assertEqual(data["rates"]["data"]["display"], "£0.0160 per MB")
        self.assertIsNone(data["rates"]["mms"])  # unknown, not £0
        self.assertEqual(data["charging"]["spend_control"], "£45 data-roaming limit")
        self.assertEqual(data["destination"]["rating_zone"], "Zone 2")

    def test_remaining_balance_is_coming_soon_never_zero(self):
        load_register([included_row("FR")])
        remaining = self.quote_for("FR")["entitlement"]["remaining"]
        self.assertEqual(remaining["status"], "coming_soon")
        self.assertIsNone(remaining["data_mb"])

    def test_charged_destination(self):
        load_register([charged_row("CH")])
        data = self.quote_for("CH")
        self.assertEqual(data["outcome"], "STANDARD_CHARGES")
        self.assertIsNone(data["entitlement"])
        self.assertEqual(data["rates"]["voice_incoming"]["display"], "£0.1500 per minute")
        self.assertEqual(data["action"]["kind"], "view_rates")

    def test_switzerland_and_turkiye_never_assumed_included(self):
        load_register([included_row("FR")])
        for code in ("CH", "TR"):
            data = self.quote_for(code)
            self.assertEqual(data["status"], "TARIFF_MAPPING_PENDING")
            self.assertIsNone(data["outcome"])
            self.assertIsNone(data["rates"])

    def test_moldova_with_unknown_inclusion_is_pending(self):
        load_register([included_row("MD", inclusion="")])
        self.assertEqual(self.quote_for("MD")["status"], "TARIFF_MAPPING_PENDING")

    def test_jersey_is_its_own_destination(self):
        load_register([included_row("FR"), charged_row("JE")])
        self.assertEqual(self.quote_for("FR")["outcome"], "INCLUDED_WITH_LIMIT")
        self.assertEqual(self.quote_for("JE")["outcome"], "STANDARD_CHARGES")

    def test_maritime_network_not_supported_with_warning(self):
        load_register([charged_row("MARITIME", supplier_availability="not_available")])
        data = self.quote_for("MARITIME")
        self.assertEqual(data["outcome"], "NOT_SUPPORTED")
        self.assertEqual(data["warnings"][0]["code"], "high_risk_network")

    def test_charged_row_without_any_rate_is_pending(self):
        load_register([charged_row("US", rate_data_per_mb="", rate_voice_to_uk_per_min="",
                                   rate_voice_incoming_per_min="", rate_sms="")])
        self.assertEqual(self.quote_for("US")["status"], "TARIFF_MAPPING_PENDING")

    def test_unknown_supplier_availability_is_pending(self):
        load_register([included_row("FR", supplier_availability="")])
        self.assertEqual(self.quote_for("FR")["status"], "TARIFF_MAPPING_PENDING")

    def test_unlimited_calls_and_texts_wording(self):
        load_register([included_row("FR", included_voice_minutes="unlimited", included_sms="unlimited",
                                    included_data_mb="40960")])
        self.assertIn("up to 40 GB of data, unlimited minutes and unlimited texts", self.quote_for("FR")["message"])

    # --- line states ----------------------------------------------------------

    def test_suspended_or_pending_line(self):
        load_register([included_row("FR")])
        for status in (Line.Status.SUSPENDED, Line.Status.PENDING_ACTIVATION, Line.Status.TERMINATED):
            line = make_line(self.user, self.plan, status=status)
            data = self.quote_for("FR", line=line)
            self.assertEqual(data["status"], "LINE_NOT_ACTIVE")
            self.assertIsNone(data["entitlement"])
            self.assertEqual(data["action"]["kind"], "choose_line")

    def test_legacy_line_without_plan(self):
        load_register([included_row("FR")])
        line = make_line(self.user, None, term="")
        data = self.quote_for("FR", line=line)
        self.assertEqual(data["status"], "LEGACY_LINE_UNLINKED")
        self.assertTrue(data["message"].startswith("We need to verify this number"))

    def test_plan_without_mapping_is_pending(self):
        load_register([included_row("FR")])
        line = make_line(self.user, make_plan("Max 30GB"))
        self.assertEqual(self.quote_for("FR", line=line)["status"], "TARIFF_MAPPING_PENDING")

    # --- register state -------------------------------------------------------

    def test_no_active_register(self):
        load_register([included_row("FR")], activate=False)
        self.assertEqual(self.quote_for("FR")["status"], "TARIFF_MAPPING_PENDING")

    def test_stale_register(self):
        load_register([included_row("FR")], validation_date=timezone.localdate() - timedelta(days=91))
        data = self.quote_for("FR")
        self.assertEqual(data["status"], "STALE")
        self.assertIsNone(data["rates"])

    def test_row_level_validation_date_wins(self):
        old = (timezone.localdate() - timedelta(days=120)).isoformat()
        load_register([included_row("FR", validation_date=old)])
        self.assertEqual(self.quote_for("FR")["status"], "STALE")

    def test_unresolved_or_overlapping_rows_are_never_used(self):
        load_register([
            included_row("FR", rate_sms="abc"),
            included_row("ES"), included_row("ES", effective_from="2026-06-01"),
        ])
        self.assertEqual(self.quote_for("FR")["status"], "TARIFF_MAPPING_PENDING")
        self.assertEqual(self.quote_for("ES")["status"], "TARIFF_MAPPING_PENDING")

    def test_tariff_change_by_travel_date(self):
        load_register([
            charged_row("ES", effective_to="2026-11-30"),
            included_row("ES", effective_from="2026-12-01"),
        ])
        self.assertEqual(self.quote_for("ES", travel=date(2026, 11, 20))["outcome"], "STANDARD_CHARGES")
        self.assertEqual(self.quote_for("ES", travel=date(2026, 12, 5))["outcome"], "INCLUDED_WITH_LIMIT")

    def test_line_keeps_plan_version_from_activation_date(self):
        plan = make_plan("Plus 3GB")
        map_plan(plan, sku="PLUS3", version="v1", effective_from=date(2025, 1, 1), effective_to=date(2026, 5, 31))
        map_plan(plan, sku="PLUS3", version="v2", effective_from=date(2026, 6, 1))
        load_register([
            included_row("FR", plan_sku="PLUS3", plan_version="v1", included_data_mb="2048"),
            charged_row("FR", plan_sku="PLUS3", plan_version="v2"),
        ])
        old_line = make_line(self.user, plan, activated=date(2026, 3, 1))
        new_line = make_line(self.user, plan, activated=date(2026, 7, 1))
        self.assertEqual(self.quote_for("FR", line=old_line)["outcome"], "INCLUDED_WITH_LIMIT")
        self.assertEqual(self.quote_for("FR", line=new_line)["outcome"], "STANDARD_CHARGES")

    def test_exact_term_mapping_beats_any_term(self):
        map_plan(self.plan, sku="STD10-ANY", term="")
        load_register([included_row("FR"), charged_row("FR", plan_sku="STD10-ANY")])
        self.assertEqual(self.quote_for("FR")["outcome"], "INCLUDED_WITH_LIMIT")

    # --- domestic / audit -----------------------------------------------------

    def test_uk_is_not_roaming(self):
        with self.assertRaises(quote.QuoteError) as ctx:
            quote.get_destination("gb")
        self.assertEqual(ctx.exception.code, "DOMESTIC_DESTINATION")

    def test_every_quote_is_saved_with_its_register_version(self):
        version = load_register([included_row("FR")])
        data = self.quote_for("FR")
        saved = RoamingQuote.objects.get(uuid=data["quote_id"])
        self.assertEqual((saved.register_version, saved.status, saved.outcome),
                         (version, "READY_PERSONALISED", "INCLUDED_WITH_LIMIT"))
        self.assertEqual(saved.response["travel_date"], "2026-12-01")

    def test_msisdn_is_masked(self):
        load_register([included_row("FR")])
        self.assertEqual(self.quote_for("FR")["line"]["msisdn_masked"], "•" * 9 + "123")


class PublicQuoteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_roaming_destinations", stdout=io.StringIO())

    def test_published_rates_are_marked_not_personalised(self):
        load_register([charged_row("US", plan_sku="", plan_version="", rating_zone="Zone 4")])
        data = quote.public_quote(dest("US"), TRAVEL).data
        self.assertEqual(data["status"], "PUBLIC_RATES_ONLY")
        self.assertFalse(data["personalised"])
        self.assertTrue(data["message"].startswith("Not a personalised quote"))
        self.assertEqual(data["destination"]["rating_zone"], "Zone 4")

    def test_plan_rows_are_not_used_as_published_rates(self):
        load_register([included_row("FR")])
        data = quote.public_quote(dest("FR"), TRAVEL).data
        self.assertEqual(data["status"], "TARIFF_MAPPING_PENDING")
        self.assertIsNone(data["rates"])
