from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.add_line.services import pac
from apps.coupons.models import Coupon
from apps.plans.models import Plan

from apps.switch import services
from apps.switch.models import SwitchRequest, SwitchStatusEvent, generate_reference

KEY = Fernet.generate_key().decode()
CREATE_URL = "/api/switch-requests/"
QUOTE_URL = "/api/v1/switch/quote/"


def make_plan(**kwargs):
    defaults = dict(
        name="Zoiko Max 30GB", price=Decimal("12.00"), price_30=Decimal("10.00"),
        price_12=Decimal("9.00"), price_24=Decimal("8.00"), duration_days=30,
        transatelID="TSL_UK_30GB",
    )
    return Plan.objects.create(**{**defaults, **kwargs})


@override_settings(PAC_ENCRYPTION_KEY=KEY)
class SwitchTestBase(TestCase):
    def setUp(self):
        cache.clear()  # throttles
        self.client = APIClient()
        self.plan = make_plan()

    def payload(self, **overrides):
        data = {
            "first_name": "Jane", "last_name": "Doe", "email": "jane@example.com",
            "mobile": "07700 900123", "postcode": "SW1A 1AA",
            "current_provider": "VOXI", "current_plan_cost": "15.00", "current_data_allowance": None,
            "plan_id": self.plan.pk, "contract_term": "12M", "sim_type": "esim",
            "switch_route": "keep_pac", "pac_code": "abc 123456", "pac_later": False,
            "promo_code": "", "opt_in_news": True, "opt_in_texts": False,
            "transfer_authorised": True, "timeline_acknowledged": True, "terms_accepted": True,
        }
        data.update(overrides)
        return data

    def submit(self, **overrides):
        return self.client.post(CREATE_URL, self.payload(**overrides), format="json")


class CreateTests(SwitchTestBase):
    def test_guest_submit_returns_reference_and_hides_code(self):
        resp = self.submit()
        self.assertEqual(resp.status_code, 201, resp.data)
        data = resp.data["data"]
        self.assertTrue(resp.data["success"])
        self.assertRegex(data["reference"], r"^ZM-SW-[A-HJ-NP-Z2-9]{8}$")
        self.assertNotIn("pac_code", data)
        self.assertNotIn("stac_code", data)
        self.assertNotIn("port_number", data)
        self.assertEqual(data["port_number_masked"], "07*** ***123")
        self.assertEqual(data["pac_status"], "received")
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["milestone"], 3)
        self.assertEqual(data["plan"], {"id": self.plan.pk, "name": "Zoiko Max 30GB"})
        self.assertEqual(len(data["milestones"]), 7)

        sr = SwitchRequest.objects.get()
        self.assertIsNone(sr.user)
        self.assertEqual(sr.selected_plan, "Zoiko Max 30GB")
        self.assertEqual(sr.port_number, "447700900123")
        self.assertEqual(sr.code_type, "PAC")
        self.assertNotIn("ABC123456", sr.encrypted_code)
        self.assertEqual(pac.decrypt(sr.encrypted_code), "ABC123456")
        self.assertEqual(
            list(sr.events.values_list("kind", flat=True)),
            [SwitchStatusEvent.Kind.CREATED, SwitchStatusEvent.Kind.CODE_RECEIVED],
        )
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn(sr.reference, mail.outbox[0].subject)

    def test_token_links_user(self):
        user = User.objects.create_user("jane", "jane@example.com", "pw")
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(self.submit().status_code, 201)
        self.assertEqual(SwitchRequest.objects.get().user, user)

    def test_code_required_unless_later(self):
        resp = self.submit(pac_code="")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("pac_code", resp.data)

        resp = self.submit(pac_code="", pac_later=True)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["data"]["pac_status"], "awaiting_code")
        self.assertEqual(resp.data["data"]["milestone"], 1)

    def test_bad_code_format_is_field_error(self):
        resp = self.submit(pac_code="123456789")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("ABC123456", resp.data["pac_code"][0])

    def test_code_must_match_route(self):
        resp = self.submit(pac_code="", stac_code="123456ABC")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("stac_code", resp.data)

        resp = self.submit(switch_route="new_stac", pac_code="", stac_code="123456abc")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(SwitchRequest.objects.get().code_type, "STAC")

    def test_new_keep_needs_no_code(self):
        resp = self.submit(switch_route="new_keep", pac_code="")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["data"]["pac_status"], "not_required")
        self.assertEqual(resp.data["data"]["milestone"], 3)

        resp = self.submit(switch_route="new_keep", pac_code="ABC123456")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("pac_code", resp.data)

    def test_providers(self):
        self.assertEqual(self.submit(current_provider="Lycamobile").status_code, 201)
        self.submit(current_provider="Other")
        self.assertTrue(SwitchRequest.objects.filter(current_provider="Other UK Provider").exists())
        resp = self.submit(current_provider="Nokia Net")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("current_provider", resp.data)

    def test_plan_must_be_active(self):
        old = make_plan(name="Old", is_active=False)
        resp = self.submit(plan_id=old.pk)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("plan_id", resp.data)

    def test_promo_validation(self):
        resp = self.submit(promo_code="NOPE")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("promo_code", resp.data)

        # New-customer promos apply to a switch (not limited to additional lines).
        Coupon.objects.create(name="WELCOME", slug="welcome", discount=Decimal("5.00"))
        self.assertEqual(self.submit(promo_code="welcome").status_code, 201)

    def test_missing_encryption_key_refuses_code_and_saves_nothing(self):
        with override_settings(PAC_ENCRYPTION_KEY=""):
            resp = self.submit()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.data["code"], "CODES_UNAVAILABLE")
        self.assertFalse(SwitchRequest.objects.exists())


class LookupTests(SwitchTestBase):
    def setUp(self):
        super().setUp()
        self.ref = self.submit().data["data"]["reference"]

    def url(self, ref=None):
        return f"/api/switch-requests/by-reference/{ref or self.ref}/"

    def test_reference_and_email(self):
        resp = self.client.get(self.url(), {"email": "JANE@example.com "})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["data"]["reference"], self.ref)
        self.assertNotIn("email", resp.data["data"])
        self.assertEqual(resp.data["data"]["history"][0]["event"], "created")

    def test_wrong_email_and_unknown_reference_look_the_same(self):
        wrong = self.client.get(self.url(), {"email": "other@example.com"})
        unknown = self.client.get(self.url("ZM-SW-AAAAAAAA"), {"email": "jane@example.com"})
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(wrong.data, unknown.data)

    def test_owner_needs_no_email_and_can_list(self):
        user = User.objects.create_user("jane", "jane@example.com", "pw")
        SwitchRequest.objects.update(user=user)
        self.client.force_authenticate(user)
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        resp = self.client.get("/api/switch-requests/mine/")
        self.assertEqual([r["reference"] for r in resp.data["data"]], [self.ref])

        other = User.objects.create_user("bob", "bob@example.com", "pw")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get("/api/switch-requests/mine/").data["data"], [])
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_mine_requires_login(self):
        self.assertEqual(self.client.get("/api/switch-requests/mine/").status_code, 401)

    def test_lookup_is_rate_limited(self):
        with mock.patch("apps.switch.views.SwitchLookupThrottle.rate", "2/hour"):
            codes = [self.client.get(self.url(), {"email": "x@example.com"}).status_code for _ in range(3)]
        self.assertEqual(codes, [404, 404, 429])

    def test_staff_endpoints_still_staff_only(self):
        self.assertEqual(self.client.get("/api/switch-requests/all/").status_code, 401)


class PortCodeTests(SwitchTestBase):
    def setUp(self):
        super().setUp()
        self.ref = self.submit(pac_code="", pac_later=True).data["data"]["reference"]
        self.url = f"/api/switch-requests/{self.ref}/port-code/"

    def post(self, **overrides):
        body = {"code_type": "PAC", "code": "xyz654321", "msisdn": "07700900123",
                "email": "jane@example.com", **overrides}
        return self.client.post(self.url, body, format="json")

    def test_code_stored_encrypted(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["data"]["pac_status"], "received")
        self.assertEqual(resp.data["data"]["milestone"], 3)
        sr = SwitchRequest.objects.get()
        self.assertEqual(pac.decrypt(sr.encrypted_code), "XYZ654321")
        self.assertFalse(sr.pac_later)
        self.assertEqual(sr.status, "pending")
        self.assertTrue(sr.events.filter(kind="code_received").exists())

    def test_wrong_email_404(self):
        self.assertEqual(self.post(email="x@example.com").status_code, 404)
        self.assertEqual(SwitchRequest.objects.get().encrypted_code, "")

    def test_field_errors(self):
        resp = self.post(code_type="STAC", code="123456ABC")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("code_type", resp.data)
        resp = self.post(code="nope")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("code", resp.data)
        resp = self.post(msisdn="0123")
        self.assertIn("msisdn", resp.data)

    def test_closed_request_rejected(self):
        SwitchRequest.objects.update(status="cancelled")
        self.assertEqual(self.post().status_code, 409)

    def test_new_keep_rejects_codes(self):
        ref = self.submit(switch_route="new_keep", pac_code="").data["data"]["reference"]
        resp = self.client.post(f"/api/switch-requests/{ref}/port-code/",
                                {"code_type": "PAC", "code": "ABC123456", "email": "jane@example.com"},
                                format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("code_type", resp.data)


class FeedbackTests(SwitchTestBase):
    def test_feedback_once(self):
        ref = self.submit().data["data"]["reference"]
        url = f"/api/switch-requests/{ref}/feedback/"
        body = {"rating": 5, "comments": "Easy!", "email": "jane@example.com"}
        self.assertEqual(self.client.post(url, {**body, "rating": 6}, format="json").status_code, 400)
        resp = self.client.post(url, body, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["data"]["rating"], 5)
        self.assertEqual(self.client.post(url, body, format="json").status_code, 409)
        status = self.client.get(f"/api/switch-requests/by-reference/{ref}/", {"email": "jane@example.com"})
        self.assertTrue(status.data["data"]["feedback_submitted"])


class QuoteTests(SwitchTestBase):
    def test_guest_quote(self):
        resp = self.client.post(QUOTE_URL, {"plan_id": self.plan.pk, "term": "24M", "sim_type": "psim"},
                                format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["quote"]["monthly_price"], "8.00")
        self.assertIsNone(resp.data["quote_token"])
        self.assertIn("contract_version", resp.data["contract"])

    def test_promo_errors_name_the_field(self):
        resp = self.client.post(QUOTE_URL, {"plan_id": self.plan.pk, "term": "24M", "sim_type": "psim",
                                            "promo_code": "NOPE"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "PROMO_NOT_FOUND")
        self.assertEqual(resp.data["field"], "promo_code")
        self.assertEqual(resp.data["promo_code"], ["We couldn't find that promo code."])

    def test_new_customer_promo_applies(self):
        Coupon.objects.create(name="WELCOME", slug="welcome", discount=Decimal("2.00"))
        resp = self.client.post(QUOTE_URL, {"plan_id": self.plan.pk, "term": "30D", "sim_type": "esim",
                                            "promo_code": "WELCOME"}, format="json")
        self.assertEqual(resp.data["quote"]["charge_today"], "8.00")


class StatusTests(SwitchTestBase):
    def setUp(self):
        super().setUp()
        self.submit()
        self.sr = SwitchRequest.objects.get()

    def test_status_change_recorded_and_completion(self):
        self.sr.status = SwitchRequest.Status.UNDER_REVIEW
        self.sr.save()
        sr = SwitchRequest.objects.get()
        sr.status = SwitchRequest.Status.COMPLETED
        sr.save(update_fields=["status"])
        sr.refresh_from_db()
        self.assertIsNotNone(sr.completed_at)
        self.assertEqual(
            list(sr.events.filter(kind="status_changed").values_list("status_before", "status_after")),
            [("pending", "under_review"), ("under_review", "completed")],
        )
        steps = services.milestones(sr)
        self.assertEqual(services.current_milestone(steps), 7)
        self.assertEqual(services.estimated_completion_date(sr), timezone.localdate(sr.completed_at))

    def test_saving_without_status_change_adds_no_event(self):
        sr = SwitchRequest.objects.get()
        sr.first_name = "Janet"
        sr.save()
        self.assertEqual(sr.events.filter(kind="status_changed").count(), 0)

    def test_estimate_skips_weekends(self):
        friday = timezone.make_aware(timezone.datetime(2026, 11, 13, 10, 0))
        SwitchRequest.objects.update(code_received_at=friday)
        sr = SwitchRequest.objects.get()
        self.assertEqual(services.estimated_completion_date(sr), date(2026, 11, 18))
        sr.estimated_completion_date = date(2026, 12, 1)
        self.assertEqual(services.estimated_completion_date(sr), date(2026, 12, 1))

    def test_purge(self):
        old = timezone.now() - timedelta(days=40)
        SwitchRequest.objects.update(code_received_at=old)
        call_command("purge_switch_codes", stdout=StringIO())
        sr = SwitchRequest.objects.get()
        self.assertEqual(sr.encrypted_code, "")
        self.assertIsNotNone(sr.code_purged_at)
        self.assertEqual(services.pac_status(sr), "expired")


@override_settings(PAC_ENCRYPTION_KEY=KEY)
class MigrationBackfillTests(TransactionTestCase):
    """Existing rows get unique references, history, and encrypted codes."""

    def test_backfill(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("switch", "0001_initial")])
        old_apps = executor.loader.project_state([("switch", "0001_initial")]).apps
        Old = old_apps.get_model("switch", "SwitchRequest")
        common = dict(first_name="A", last_name="B", email="a@example.com", mobile="07700900123",
                      postcode="X", selected_plan="Zoiko Max 30GB")
        Old.objects.create(**common, pac_code="abc123456", current_provider="Other")
        Old.objects.create(**common)

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes("switch"))

        rows = list(SwitchRequest.objects.order_by("pk"))
        self.assertEqual(len({r.reference for r in rows}), 2)
        self.assertEqual(rows[0].current_provider, "Other UK Provider")
        self.assertEqual(rows[0].code_type, "PAC")
        self.assertEqual(rows[0].switch_route, "keep_pac")
        self.assertEqual(pac.decrypt(rows[0].encrypted_code), "ABC123456")
        self.assertEqual(rows[1].encrypted_code, "")
        self.assertEqual(SwitchStatusEvent.objects.filter(kind="created").count(), 2)


class ReferenceTests(TestCase):
    def test_format_and_randomness(self):
        refs = {generate_reference() for _ in range(50)}
        self.assertEqual(len(refs), 50)
        for ref in refs:
            self.assertRegex(ref, r"^ZM-SW-[A-HJ-NP-Z2-9]{8}$")
