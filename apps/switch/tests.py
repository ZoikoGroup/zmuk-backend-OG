from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.add_line.models import AddLineOrder, Line, PortRequest
from apps.add_line.services import pac
from apps.coupons.models import Coupon
from apps.plans.models import Plan
from apps.switch import services
from apps.switch.models import SwitchRequest, SwitchStatusEvent, generate_reference

KEY = Fernet.generate_key().decode()
CREATE_URL = "/api/switch-requests/"
QUOTE_URL = "/api/v1/switch/quote/"
S = SwitchRequest.Status


def make_plan(**kwargs):
    defaults = dict(
        name="Zoiko Max 30GB", price=Decimal("12.00"), price_30=Decimal("10.00"),
        price_12=Decimal("9.00"), price_24=Decimal("8.00"), duration_days=30,
        transatelID="TSL_UK_30GB",
    )
    return Plan.objects.create(**{**defaults, **kwargs})


@override_settings(PAC_ENCRYPTION_KEY=KEY, SWITCH_CONSENT_VERSION="2026-11-12")
class SwitchTestBase(TestCase):
    def setUp(self):
        cache.clear()  # throttles
        self.client = APIClient()
        self.plan = make_plan()

    def payload(self, **overrides):
        data = {
            "first_name": "Jane", "last_name": "Doe", "email": "jane@example.com",
            "mobile": "07700 900123", "address_line_1": "10 Example Street", "address_line_2": "",
            "town_city": "London", "postcode": "SW1A 1AA",
            "current_provider": "VOXI", "current_plan_cost": "15.00", "current_data_allowance": None,
            "plan_id": self.plan.pk, "contract_term": "12M", "sim_type": "esim",
            "switch_route": "keep_pac", "pac_code": "abc 123456", "pac_later": False,
            "promo_code": "", "opt_in_news": True, "opt_in_texts": False,
            "transfer_authorised": True, "timeline_acknowledged": True, "terms_accepted": True,
            "consent_version": "2026-11-12",
        }
        data.update(overrides)
        return data

    def submit(self, headers=None, **overrides):
        return self.client.post(CREATE_URL, self.payload(**overrides), format="json", headers=headers or {})


class CreateTests(SwitchTestBase):
    def test_guest_submit(self):
        resp = self.submit()
        self.assertEqual(resp.status_code, 201, resp.data)
        data = resp.data["data"]
        self.assertTrue(resp.data["success"])
        self.assertFalse(resp.data["duplicate"])
        self.assertIn(f"Your reference is {data['reference']}", resp.data["message"])
        self.assertRegex(data["reference"], r"^ZM-SW-[A-HJ-NP-Z2-9]{8}$")
        for hidden in ("pac_code", "stac_code", "port_number", "email", "mobile", "encrypted_code",
                       "address_line_1", "town_city", "postcode"):
            self.assertNotIn(hidden, data)
        self.assertEqual(data["status"], "validating")
        self.assertEqual(data["stage"], {"key": "request_received", "label": "Request received"})
        self.assertEqual([s["key"] for s in data["steps"]],
                         ["request_received", "order_placed", "sim_ready", "switch_scheduled", "number_transferred"])
        self.assertEqual([s["done"] for s in data["steps"]], [True, False, False, False, False])
        self.assertIsNone(data["action_needed"])
        self.assertEqual(data["next_action"], "none")
        self.assertEqual(data["pac_status"], "received")
        self.assertIsNone(data["switch_date"])
        self.assertEqual(data["switch_date_message"], services.NO_DATE_MESSAGE)
        self.assertEqual(data["port_number_masked"], "07*** ***123")
        self.assertEqual([h["event"] for h in data["history"]], ["created", "code_received", "status_changed"])

        sr = SwitchRequest.objects.get()
        self.assertIsNone(sr.user)
        self.assertEqual(sr.selected_plan, "Zoiko Max 30GB")
        self.assertEqual(sr.port_number, "447700900123")
        self.assertEqual(pac.decrypt(sr.encrypted_code), "ABC123456")
        # Quote snapshot and consent evidence (G2, G4).
        self.assertEqual(sr.quote_snapshot["quote"]["monthly_price"], "9.00")
        self.assertEqual(len(sr.quote_hash), 64)
        self.assertEqual(sr.consent_version, "2026-11-12")
        self.assertIsNotNone(sr.consented_at)
        self.assertEqual(sr.consent_ip, "127.0.0.1")

    def test_emails_are_masked(self):
        self.submit()
        sr = SwitchRequest.objects.get()
        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            html = message.alternatives[0][0]
            self.assertNotIn("07700900123", html)
            self.assertNotIn("447700900123", html)
            self.assertNotIn("ABC123456", html)
            self.assertNotIn("10 Example Street", html)
            self.assertIn(sr.reference, html)
        self.assertNotIn("jane@example.com", mail.outbox[1].alternatives[0][0])
        self.assertIn(f"/admin/switch/switchrequest/{sr.pk}/change/", mail.outbox[1].alternatives[0][0])
        self.assertNotIn("1 working day", mail.outbox[0].alternatives[0][0])

    def test_code_later_is_action_needed(self):
        resp = self.submit(pac_code="", pac_later=True)
        self.assertEqual(resp.status_code, 201, resp.data)
        data = resp.data["data"]
        self.assertEqual(data["status"], "action_needed")
        self.assertEqual(data["stage"]["key"], "action_needed")
        self.assertEqual(data["action_needed"]["reason"], "code_needed")
        self.assertIn("PAC", data["action_needed"]["message"])
        self.assertEqual(data["next_action"], "provide_code")
        self.assertEqual(data["pac_status"], "awaiting_code")

    def test_token_links_user(self):
        user = User.objects.create_user("jane", "jane@example.com", "pw")
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(self.submit().status_code, 201)
        sr = SwitchRequest.objects.get()
        self.assertEqual(sr.user, user)
        self.assertFalse(sr.existing_account_email)

    def test_existing_account_email_is_recorded_not_linked_or_revealed(self):
        User.objects.create_user("jane", "JANE@example.com", "pw")
        resp = self.submit()
        sr = SwitchRequest.objects.get()
        self.assertIsNone(sr.user)
        self.assertTrue(sr.existing_account_email)
        self.assertNotIn("existing_account_email", resp.data["data"])
        self.assertEqual(resp.data["data"]["next_action"], "none")

    def test_consent_version_must_be_current(self):
        resp = self.submit(consent_version="2025-01-01")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("consent_version", resp.data)

    def test_code_required_unless_later(self):
        resp = self.submit(pac_code="")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("pac_code", resp.data)

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
        self.assertEqual(resp.data["data"]["steps"][4]["label"], "Old service closed")

    def test_new_keep_needs_no_code(self):
        resp = self.submit(switch_route="new_keep", pac_code="")
        self.assertEqual(resp.status_code, 201, resp.data)
        data = resp.data["data"]
        self.assertEqual(data["status"], "validating")
        self.assertEqual(data["pac_status"], "not_required")
        self.assertEqual([s["applicable"] for s in data["steps"]], [True, True, True, False, False])

        resp = self.submit(switch_route="new_keep", pac_code="ABC123456", email="b@example.com")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("pac_code", resp.data)

    def test_providers(self):
        self.assertEqual(self.submit(current_provider="Lycamobile").status_code, 201)
        self.submit(current_provider="Other", email="b@example.com")
        self.assertTrue(SwitchRequest.objects.filter(current_provider="Other UK Provider").exists())
        resp = self.submit(current_provider="Nokia Net", email="c@example.com")
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
        self.assertEqual(SwitchRequest.objects.get().quote_snapshot["quote"]["promo"]["code"], "welcome")

    def test_missing_encryption_key_refuses_code_and_saves_nothing(self):
        with override_settings(PAC_ENCRYPTION_KEY=""):
            resp = self.submit()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.data["code"], "CODES_UNAVAILABLE")
        self.assertFalse(SwitchRequest.objects.exists())


class AddressAndContactTests(SwitchTestBase):
    """Step 3 of the 5-step journey: full address, optional contact number."""

    def test_address_is_stored_and_postcode_formatted(self):
        resp = self.submit(address_line_2="Flat 2", postcode=" sw1a1aa ")
        self.assertEqual(resp.status_code, 201, resp.data)
        sr = SwitchRequest.objects.get()
        self.assertEqual((sr.address_line_1, sr.address_line_2, sr.town_city, sr.postcode),
                         ("10 Example Street", "Flat 2", "London", "SW1A 1AA"))

    def test_address_fields_are_required(self):
        resp = self.submit(address_line_1=" ", town_city="")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["address_line_1"][0], "Enter the first line of your address.")
        self.assertEqual(resp.data["town_city"][0], "Enter your town or city.")

        payload = self.payload()
        del payload["address_line_1"]
        resp = self.client.post(CREATE_URL, payload, format="json")
        self.assertIn("address_line_1", resp.data)

    def test_invalid_postcode(self):
        for bad in ("12345", "SW1A", "10 Downing Street"):
            resp = self.submit(postcode=bad)
            self.assertEqual(resp.status_code, 400, bad)
            self.assertEqual(resp.data["postcode"][0], "Enter a valid UK postcode.")

    def test_contact_number_is_optional(self):
        resp = self.submit(mobile="", port_number="07700 900999")
        self.assertEqual(resp.status_code, 201, resp.data)
        sr = SwitchRequest.objects.get()
        self.assertEqual((sr.mobile, sr.port_number), ("", "447700900999"))

        payload = self.payload(email="b@example.com", port_number="07700 900888")
        del payload["mobile"]
        self.assertEqual(self.client.post(CREATE_URL, payload, format="json").status_code, 201)

    def test_contact_number_still_checked_when_given(self):
        resp = self.submit(mobile="12345")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("mobile", resp.data)

    def test_keep_number_needs_the_number(self):
        resp = self.submit(mobile="", port_number="")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["port_number"][0], "Enter the mobile number you want to bring to Zoiko.")

    def test_new_keep_needs_no_number(self):
        resp = self.submit(switch_route="new_keep", pac_code="", mobile="")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(SwitchRequest.objects.get().port_number, "")

    def test_code_later_without_any_number_asks_for_it(self):
        resp = self.submit(switch_route="new_stac", pac_code="", pac_later=True, mobile="")
        self.assertEqual(resp.status_code, 201, resp.data)
        ref = resp.data["data"]["reference"]
        url = f"/api/switch-requests/{ref}/port-code/"
        body = {"code_type": "STAC", "code": "123456ABC", "email": "jane@example.com"}

        resp = self.client.post(url, body, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("msisdn", resp.data)

        resp = self.client.post(url, {**body, "msisdn": "07700900777"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(SwitchRequest.objects.get().port_number, "447700900777")


class DuplicateTests(SwitchTestBase):
    def test_idempotency_key_replays(self):
        headers = {"Idempotency-Key": "form-session-123"}
        first = self.submit(headers=headers)
        again = self.submit(headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.data["duplicate"])
        self.assertEqual(again.data["data"]["reference"], first.data["data"]["reference"])
        self.assertEqual(SwitchRequest.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 2)  # no second set of emails

    def test_idempotency_key_reused_for_other_person(self):
        headers = {"Idempotency-Key": "form-session-123"}
        self.submit(headers=headers)
        resp = self.submit(headers=headers, email="someone@example.com")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["code"], "IDEMPOTENCY_KEY_REUSED")

    def test_bad_idempotency_key(self):
        resp = self.submit(headers={"Idempotency-Key": "x"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("idempotency_key", resp.data)

    def test_identical_submit_without_key_is_deduplicated(self):
        first = self.submit()
        again = self.submit()
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.data["duplicate"])
        self.assertEqual(again.data["data"]["reference"], first.data["data"]["reference"])
        self.assertEqual(SwitchRequest.objects.count(), 1)

        # A different plan term is a different request.
        self.assertEqual(self.submit(contract_term="24M").status_code, 201)

    def test_old_or_closed_request_is_not_a_duplicate(self):
        self.submit()
        SwitchRequest.objects.update(created_at=timezone.now() - timedelta(minutes=30))
        self.assertEqual(self.submit().status_code, 201)
        SwitchRequest.objects.update(status=S.CANCELLED)
        self.assertEqual(self.submit().status_code, 201)


class LookupTests(SwitchTestBase):
    def setUp(self):
        super().setUp()
        self.ref = self.submit().data["data"]["reference"]

    def url(self, ref=None):
        return f"/api/switch-requests/by-reference/{ref or self.ref}/"

    def test_guest_gets_status_only(self):
        resp = self.client.get(self.url(), {"email": "JANE@example.com "})
        self.assertEqual(resp.status_code, 200)
        data = resp.data["data"]
        self.assertEqual(data["reference"], self.ref)
        for personal in ("first_name", "plan", "port_number_masked", "email", "order_reference", "id"):
            self.assertNotIn(personal, data)
        self.assertEqual(data["stage"]["key"], "request_received")

    def test_wrong_email_and_unknown_reference_look_the_same(self):
        wrong = self.client.get(self.url(), {"email": "other@example.com"})
        unknown = self.client.get(self.url("ZM-SW-AAAAAAAA"), {"email": "jane@example.com"})
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(wrong.data, unknown.data)

    def test_owner_gets_full_view_and_list(self):
        user = User.objects.create_user("jane", "jane@example.com", "pw")
        SwitchRequest.objects.update(user=user)
        self.client.force_authenticate(user)
        resp = self.client.get(self.url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["data"]["first_name"], "Jane")
        self.assertEqual(resp.data["data"]["plan"]["name"], "Zoiko Max 30GB")
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

    def test_staff_endpoints(self):
        self.assertEqual(self.client.get("/api/switch-requests/all/").status_code, 401)
        staff = User.objects.create_user("ops", "ops@example.com", "pw", is_staff=True)
        self.client.force_authenticate(staff)
        resp = self.client.get(f"/api/switch-requests/{SwitchRequest.objects.get().pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("encrypted_code", resp.data)


class PortCodeTests(SwitchTestBase):
    def setUp(self):
        super().setUp()
        self.ref = self.submit(pac_code="", pac_later=True).data["data"]["reference"]
        self.url = f"/api/switch-requests/{self.ref}/port-code/"

    def post(self, **overrides):
        body = {"code_type": "PAC", "code": "xyz654321", "msisdn": "07700900123",
                "email": "jane@example.com", **overrides}
        return self.client.post(self.url, body, format="json")

    def test_code_clears_action_needed(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.data["data"]
        self.assertEqual(data["pac_status"], "received")
        self.assertEqual(data["status"], "validating")
        self.assertEqual(data["next_action"], "none")
        self.assertNotIn("first_name", data)  # guest view
        sr = SwitchRequest.objects.get()
        self.assertEqual(pac.decrypt(sr.encrypted_code), "XYZ654321")
        self.assertEqual(sr.action_reason, "")
        self.assertFalse(sr.pac_later)

    def test_code_does_not_move_other_statuses(self):
        SwitchRequest.objects.update(status=S.READY_FOR_ORDER, action_reason="")
        self.post()
        self.assertEqual(SwitchRequest.objects.get().status, S.READY_FOR_ORDER)

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

    def test_closed_and_converted_requests_rejected(self):
        SwitchRequest.objects.update(status=S.CANCELLED)
        self.assertEqual(self.post().data["code"], "REQUEST_CLOSED")
        SwitchRequest.objects.update(status=S.CONVERTED)
        self.assertEqual(self.post().data["code"], "REQUEST_CONVERTED")

    def test_new_keep_rejects_codes(self):
        ref = self.submit(switch_route="new_keep", pac_code="", email="b@example.com").data["data"]["reference"]
        resp = self.client.post(f"/api/switch-requests/{ref}/port-code/",
                                {"code_type": "PAC", "code": "ABC123456", "email": "b@example.com"},
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
        self.assertEqual(resp.data["consent_version"], "2026-11-12")

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


class StateAndTrackerTests(SwitchTestBase):
    def setUp(self):
        super().setUp()
        self.submit()
        self.sr = SwitchRequest.objects.get()

    def test_status_history_and_reason_cleared(self):
        self.sr.set_status(S.ACTION_NEEDED, reason=SwitchRequest.ActionReason.IDENTITY,
                           owner=SwitchRequest.ActionOwner.CUSTOMER)
        self.assertEqual(services.next_action(self.sr), "await_contact")
        self.sr.set_status(S.READY_FOR_ORDER)
        sr = SwitchRequest.objects.get()
        self.assertEqual((sr.action_reason, sr.action_owner), ("", ""))
        self.assertEqual(
            list(sr.events.filter(kind="status_changed").values_list(
                "status_before", "status_after", "action_reason", "action_owner")),
            [("received", "validating", "", ""), ("validating", "action_needed", "identity", "customer"),
             ("action_needed", "ready_for_order", "", "")],
        )

    def test_owner_defaults_from_reason(self):
        self.sr.set_status(S.ACTION_NEEDED, reason=SwitchRequest.ActionReason.ELIGIBILITY)
        self.assertEqual(self.sr.action_owner, "operations")
        self.sr.set_status(S.ACTION_NEEDED, reason=SwitchRequest.ActionReason.CODE_INVALID)
        self.assertEqual(self.sr.action_owner, "customer")

    def test_code_reasons_always_belong_to_the_customer(self):
        sr = SwitchRequest.objects.get()
        sr.status, sr.action_reason = S.ACTION_NEEDED, SwitchRequest.ActionReason.CODE_EXPIRED
        sr.action_owner = SwitchRequest.ActionOwner.OPERATIONS
        with self.assertRaises(ValidationError):
            sr.full_clean()
        sr.save()  # code paths that skip clean() are corrected
        self.assertEqual(SwitchRequest.objects.get().action_owner, "customer")

    def test_operations_action_is_hidden_from_customer(self):
        self.sr.set_status(S.ACTION_NEEDED, reason=SwitchRequest.ActionReason.DUPLICATE)
        resp = self.client.get(f"/api/switch-requests/by-reference/{self.sr.reference}/",
                               {"email": "jane@example.com"})
        data = resp.data["data"]
        self.assertEqual((data["status"], data["status_label"]), ("validating", "Checking your request"))
        self.assertEqual(data["stage"]["key"], "request_received")
        self.assertIsNone(data["action_needed"])
        self.assertEqual(data["next_action"], "none")
        # received → validating → (ops action, shown as validating): no repeat, no "action needed".
        self.assertEqual([h["label"] for h in data["history"]],
                         ["Request received", "Switching code received", "Checking your request"])

        # Staff still see the real state.
        self.assertEqual(SwitchRequest.objects.get().status, S.ACTION_NEEDED)

    def test_reason_change_while_action_needed_is_recorded(self):
        self.sr.set_status(S.ACTION_NEEDED, reason=SwitchRequest.ActionReason.ELIGIBILITY)
        self.sr.set_status(S.ACTION_NEEDED, reason=SwitchRequest.ActionReason.IDENTITY,
                           owner=SwitchRequest.ActionOwner.CUSTOMER)
        last = self.sr.events.last()
        self.assertEqual((last.status_after, last.action_reason, last.action_owner),
                         ("action_needed", "identity", "customer"))

    def test_status_alone_never_advances_the_tracker(self):
        self.sr.set_status(S.READY_FOR_ORDER)
        steps = services.tracker(self.sr)["steps"]
        self.assertEqual([s["done"] for s in steps], [True, False, False, False, False])

    def test_converted_requires_an_order(self):
        sr = SwitchRequest.objects.get()
        sr.status = S.CONVERTED
        with self.assertRaises(ValidationError):
            sr.full_clean()
        sr.status = S.ACTION_NEEDED
        with self.assertRaises(ValidationError):
            sr.full_clean()  # needs a reason

    def test_tracker_follows_order_line_and_port(self):
        user = User.objects.create_user("jane", "jane@example.com", "pw")
        now = timezone.now()
        line = Line.objects.create(owner=user, plan=self.plan, term="12M",
                                   status=Line.Status.ACTIVE, activated_at=now)
        order = AddLineOrder.objects.create(
            owner=user, plan=self.plan, term="12M", sim_type="esim", switch_choice="pac",
            total_gross=Decimal("9.00"), vat_amount=Decimal("1.50"), line=line,
            payment_status=AddLineOrder.PaymentStatus.PAID, paid_at=now,
        )
        port = PortRequest.objects.create(line=line, order=order, code_type="PAC",
                                          status=PortRequest.Status.VALIDATED, validated_at=now,
                                          port_date=date(2026, 11, 20))
        sr = SwitchRequest.objects.get()
        sr.order, sr.line = order, line
        sr.save()
        sr.set_status(S.CONVERTED)

        t = services.tracker(sr)
        self.assertEqual([s["done"] for s in t["steps"]], [True, True, True, True, False])
        self.assertEqual(t["stage"]["key"], "switch_scheduled")
        self.assertEqual(services.switch_date(sr), date(2026, 11, 20))
        self.assertEqual(services.pac_status(sr), "validated")

        PortRequest.objects.filter(pk=port.pk).update(status=PortRequest.Status.DONE, completed_at=now)
        self.assertEqual(services.tracker(sr)["stage"]["key"], "number_transferred")

    def test_confirmed_date_only(self):
        self.assertIsNone(services.switch_date(self.sr))
        self.sr.confirmed_switch_date = date(2026, 12, 1)
        self.assertEqual(services.switch_date(self.sr), date(2026, 12, 1))
        self.sr.status = S.CANCELLED
        self.assertIsNone(services.switch_date(self.sr))

    def test_expired_code_flagged_but_kept_while_deletion_off(self):
        SwitchRequest.objects.update(code_received_at=timezone.now() - timedelta(days=31))
        out = StringIO()
        call_command("purge_switch_codes", stdout=out)
        sr = SwitchRequest.objects.get()
        self.assertNotEqual(sr.encrypted_code, "")
        self.assertIsNone(sr.code_purged_at)
        self.assertEqual((sr.status, sr.action_reason, sr.action_owner),
                         (S.ACTION_NEEDED, "code_expired", "customer"))
        self.assertEqual(services.pac_status(sr), "expired")
        self.assertEqual(services.next_action(sr), "provide_code")

        # Running again doesn't add more history.
        events = sr.events.count()
        call_command("purge_switch_codes", stdout=StringIO())
        self.assertEqual(sr.events.count(), events)

    def test_code_within_30_days_is_left_alone(self):
        SwitchRequest.objects.update(code_received_at=timezone.now() - timedelta(days=29))
        call_command("purge_switch_codes", stdout=StringIO())
        self.assertEqual(SwitchRequest.objects.get().status, S.VALIDATING)

    def test_deletion_off_only_reports(self):
        SwitchRequest.objects.update(code_received_at=timezone.now() - timedelta(days=400))
        out = StringIO()
        call_command("purge_switch_codes", stdout=out)
        self.assertNotEqual(SwitchRequest.objects.get().encrypted_code, "")
        self.assertIn("deletion is off", out.getvalue())

    @override_settings(SWITCH_CODE_DELETION_ENABLED=True, PAC_RETENTION_DAYS=30)
    def test_deletion_when_enabled_removes_only_the_code(self):
        SwitchRequest.objects.update(code_received_at=timezone.now() - timedelta(days=61))
        call_command("purge_switch_codes", stdout=StringIO())
        sr = SwitchRequest.objects.get()
        self.assertEqual(sr.encrypted_code, "")
        self.assertIsNotNone(sr.code_purged_at)
        self.assertEqual(sr.email, "jane@example.com")
        self.assertEqual(services.pac_status(sr), "expired")

    def test_dry_run_changes_nothing(self):
        SwitchRequest.objects.update(code_received_at=timezone.now() - timedelta(days=31))
        call_command("purge_switch_codes", "--dry-run", stdout=StringIO())
        self.assertEqual(SwitchRequest.objects.get().status, S.VALIDATING)

    def test_nothing_ever_expires_or_deletes_requests_automatically(self):
        SwitchRequest.objects.update(created_at=timezone.now() - timedelta(days=900),
                                     code_received_at=timezone.now() - timedelta(days=900))
        with override_settings(SWITCH_CODE_DELETION_ENABLED=True):
            call_command("purge_switch_codes", stdout=StringIO())
        self.assertEqual(SwitchRequest.objects.count(), 1)
        self.assertNotEqual(SwitchRequest.objects.get().status, S.EXPIRED)


class AdminTests(SwitchTestBase):
    def test_bulk_action_records_actor_and_order_fields_locked(self):
        self.submit()
        sr = SwitchRequest.objects.get()
        admin_user = User.objects.create_superuser("ops", "ops@example.com", "pw")
        self.client.force_login(admin_user)
        resp = self.client.post("/admin/switch/switchrequest/", {
            "action": "mark_ready_for_order", "_selected_action": [sr.pk],
        })
        self.assertEqual(resp.status_code, 302)
        sr.refresh_from_db()
        self.assertEqual(sr.status, S.READY_FOR_ORDER)
        self.assertEqual(sr.events.last().actor, admin_user)

        page = self.client.get(f"/admin/switch/switchrequest/{sr.pk}/change/")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('name="order"', page.content.decode())

    def test_queue_filter_splits_customer_and_operations(self):
        self.submit()
        self.submit(email="sam@example.com", mobile="07700900456")
        ops_sr, cust_sr = SwitchRequest.objects.order_by("pk")
        ops_sr.set_status(S.ACTION_NEEDED, reason=SwitchRequest.ActionReason.ELIGIBILITY)
        cust_sr.set_status(S.ACTION_NEEDED, reason=SwitchRequest.ActionReason.CODE_NEEDED)
        self.client.force_login(User.objects.create_superuser("ops", "ops@example.com", "pw"))

        ops_page = self.client.get("/admin/switch/switchrequest/", {"queue": "operations"}).content.decode()
        cust_page = self.client.get("/admin/switch/switchrequest/", {"queue": "customer"}).content.decode()
        self.assertIn(ops_sr.reference, ops_page)
        self.assertNotIn(cust_sr.reference, ops_page)
        self.assertIn(cust_sr.reference, cust_page)
        self.assertNotIn(ops_sr.reference, cust_page)


@override_settings(PAC_ENCRYPTION_KEY=KEY)
class MigrationTests(TransactionTestCase):
    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def test_phase1_backfill_from_0001(self):
        old_apps = self._migrate([("switch", "0001_initial")])
        Old = old_apps.get_model("switch", "SwitchRequest")
        common = dict(first_name="A", last_name="B", email="a@example.com", mobile="07700900123",
                      postcode="X", selected_plan="Zoiko Max 30GB")
        Old.objects.create(**common, pac_code="abc123456", current_provider="Other")
        Old.objects.create(**common, status="completed")

        self._migrate(MigrationExecutor(connection).loader.graph.leaf_nodes("switch"))
        rows = list(SwitchRequest.objects.order_by("pk"))
        self.assertEqual(len({r.reference for r in rows}), 2)
        self.assertEqual(rows[0].current_provider, "Other UK Provider")
        self.assertEqual(rows[0].switch_route, "keep_pac")
        self.assertEqual(pac.decrypt(rows[0].encrypted_code), "ABC123456")
        self.assertEqual(rows[0].status, "validating")
        self.assertEqual(rows[1].status, "converted")
        self.assertIn("Legacy", rows[1].ops_note)
        self.assertEqual(
            set(SwitchStatusEvent.objects.values_list("status_after", flat=True)), {"validating", "converted"}
        )

    def test_status_remap_from_0004(self):
        old_apps = self._migrate([("switch", "0004_drop_plain_codes_unique_reference")])
        Old = old_apps.get_model("switch", "SwitchRequest")
        common = dict(first_name="A", last_name="B", email="a@example.com", mobile="07700900123",
                      postcode="X")
        rows = {
            "needs_code": Old.objects.create(**common, reference="ZM-SW-AAAAAAA1", status="pending",
                                             switch_route="keep_pac", pac_later=True),
            "pending": Old.objects.create(**common, reference="ZM-SW-AAAAAAA2", status="pending",
                                          switch_route="new_keep"),
            "processing": Old.objects.create(**common, reference="ZM-SW-AAAAAAA3", status="processing",
                                             estimated_completion_date=date(2026, 12, 1)),
            "failed": Old.objects.create(**common, reference="ZM-SW-AAAAAAA4", status="failed"),
        }
        self._migrate(MigrationExecutor(connection).loader.graph.leaf_nodes("switch"))
        new = {k: SwitchRequest.objects.get(pk=v.pk) for k, v in rows.items()}
        self.assertEqual((new["needs_code"].status, new["needs_code"].action_reason, new["needs_code"].action_owner),
                         ("action_needed", "code_needed", "customer"))
        self.assertEqual(new["pending"].status, "validating")
        self.assertEqual(new["processing"].status, "ready_for_order")
        self.assertEqual(new["processing"].confirmed_switch_date, date(2026, 12, 1))
        self.assertEqual(new["failed"].status, "rejected")
        self.assertTrue(new["needs_code"].events.filter(status_after="action_needed",
                                                        action_owner="customer").exists())

    def test_action_owner_backfill_from_0006(self):
        old_apps = self._migrate([("switch", "0006_remap_request_statuses")])
        Old = old_apps.get_model("switch", "SwitchRequest")
        common = dict(first_name="A", last_name="B", email="a@example.com", mobile="07700900123",
                      postcode="X", status="action_needed")
        code = Old.objects.create(**common, reference="ZM-SW-BBBBBBB1", action_reason="code_expired")
        ops = Old.objects.create(**common, reference="ZM-SW-BBBBBBB2", action_reason="identity")
        other = Old.objects.create(**{**common, "status": "validating"}, reference="ZM-SW-BBBBBBB3")

        self._migrate(MigrationExecutor(connection).loader.graph.leaf_nodes("switch"))
        self.assertEqual(SwitchRequest.objects.get(pk=code.pk).action_owner, "customer")
        self.assertEqual(SwitchRequest.objects.get(pk=ops.pk).action_owner, "operations")
        self.assertEqual(SwitchRequest.objects.get(pk=other.pk).action_owner, "")

    def test_legacy_free_text_address_is_split_from_0008(self):
        old_apps = self._migrate([("switch", "0008_backfill_action_owner")])
        Old = old_apps.get_model("switch", "SwitchRequest")
        common = dict(first_name="A", last_name="B", email="a@example.com", mobile="07700900123")
        rows = {
            "full": Old.objects.create(**common, reference="ZM-SW-CCCCCCC1",
                                       postcode="10 Downing Street, London sw1a2aa"),
            "only": Old.objects.create(**common, reference="ZM-SW-CCCCCCC2", postcode="M11AE"),
            "none": Old.objects.create(**common, reference="ZM-SW-CCCCCCC3", postcode="Somewhere without one"),
        }
        self._migrate(MigrationExecutor(connection).loader.graph.leaf_nodes("switch"))
        new = {k: SwitchRequest.objects.get(pk=v.pk) for k, v in rows.items()}
        self.assertEqual((new["full"].address_line_1, new["full"].postcode),
                         ("10 Downing Street, London", "SW1A 2AA"))
        self.assertEqual((new["only"].address_line_1, new["only"].postcode), ("", "M1 1AE"))
        self.assertEqual((new["none"].address_line_1, new["none"].postcode), ("Somewhere without one", ""))


class ReferenceTests(TestCase):
    def test_format_and_randomness(self):
        refs = {generate_reference() for _ in range(50)}
        self.assertEqual(len(refs), 50)
        for ref in refs:
            self.assertRegex(ref, r"^ZM-SW-[A-HJ-NP-Z2-9]{8}$")
