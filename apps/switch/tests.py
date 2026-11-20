import re
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import Permission, User
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
from apps.add_line.services import pac, payments
from apps.coupons.models import Coupon
from apps.plans.models import Plan
from apps.sims.models import Sim
from apps.switch import continuation, documents, ordering, receipts, services, wording
from apps.switch.models import (
    ContractTermRule,
    ContractTerms,
    ImmutableRecord,
    PlanPriceChange,
    PlanTariff,
    SwitchContinuationToken,
    SwitchContractConsent,
    SwitchContractDocument,
    SwitchContractPack,
    SwitchContractProvision,
    SwitchDocumentDelivery,
    SwitchPaymentCapture,
    SwitchPaymentReceipt,
    SwitchRequest,
    SwitchStatusEvent,
    generate_reference,
)

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


@override_settings(PAC_ENCRYPTION_KEY=KEY, SWITCH_CONSENT_VERSION="2026-11-20")
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
            "consent_version": "2026-11-20",
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
        self.assertEqual(sr.consent_version, "2026-11-20")
        self.assertEqual(sr.consent_statement, wording.REQUEST_AUTHORISATION["2026-11-20"])  # exact text (C8)
        self.assertIsNotNone(sr.consented_at)
        self.assertEqual(sr.consent_ip, "127.0.0.1")

    def test_only_the_switching_authorisation_is_required(self):
        """v2.3 §4: one mandatory box. Step 4 doesn't ask the customer to
        accept contract terms; that happens in Part B."""
        resp = self.submit(timeline_acknowledged=False, terms_accepted=False)
        self.assertEqual(resp.status_code, 201, resp.data)
        SwitchRequest.objects.all().delete()
        resp = self.submit(transfer_authorised=False)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("transfer_authorised", resp.data)
        self.assertFalse(SwitchRequest.objects.exists())

    def test_wording_version_must_exist(self):
        self.assertEqual(wording.check_consent_wording(), [])
        with override_settings(SWITCH_CONSENT_VERSION="1999-01-01"):
            self.assertEqual([e.id for e in wording.check_consent_wording()], ["switch.E001"])

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
        self.assertEqual(resp.data["consent_version"], "2026-11-20")
        # Step 4 shows the server's sentence, not a copy of its own (C8).
        self.assertEqual(resp.data["consent_statement"], wording.REQUEST_AUTHORISATION["2026-11-20"])
        self.assertIn("does not place a paid order", resp.data["consent_statement"])

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


@override_settings(SWITCH_ORDER_CONVERSION_ENABLED=True, ADD_LINE_SUPPLIER_WRITES_ENABLED=True,
                   FRONTEND_URL="https://zoiko.test", SWITCH_CONTINUATION_TTL_HOURS=48)
class ContinuationTests(SwitchTestBase):
    """Milestone 4 · B1: secure continuation link and claim."""

    CHECK = "/api/switch-requests/continue/check/"
    CLAIM = "/api/switch-requests/continue/claim/"
    RESEND = "/api/switch-requests/continue/resend/"

    def setUp(self):
        super().setUp()
        self.submit()
        self.sr = SwitchRequest.objects.get()
        self.sr.set_status(S.READY_FOR_ORDER)
        self.user = User.objects.create_user("jane", "jane@example.com", "pw")
        mail.outbox.clear()

    def issue(self):
        continuation.issue_link(self.sr)
        body = mail.outbox[-1].alternatives[0][0]
        match = re.search(r"https://zoiko\.test/switch-and-save/continue\?token=([A-Za-z0-9_\-]+)", body)
        self.assertIsNotNone(match, "link missing from email")
        return match.group(1)

    def claim(self, token, user=None):
        client = APIClient()
        client.force_authenticate(user or self.user)
        return client.post(self.CLAIM, {"token": token}, format="json")

    def test_link_is_emailed_and_only_its_hash_stored(self):
        token = self.issue()
        self.assertEqual(mail.outbox[-1].to, ["jane@example.com"])
        self.assertIn(self.sr.reference, mail.outbox[-1].subject)
        stored = SwitchContinuationToken.objects.get()
        self.assertNotEqual(stored.token_hash, token)
        self.assertEqual(len(stored.token_hash), 64)
        self.assertTrue(stored.delivered)
        self.assertEqual(stored.sent_to, "j***@example.com")
        self.assertTrue(self.sr.events.filter(kind="link_sent").exists())
        # Internal history entries aren't shown to the customer.
        self.assertNotIn("link_sent", [h["event"] for h in services.history(self.sr)])

    def test_check_signed_out(self):
        token = self.issue()
        resp = self.client.post(self.CHECK, {"token": token}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.data["data"]
        self.assertEqual(data["reference"], self.sr.reference)
        self.assertEqual(data["email_masked"], "j***@example.com")
        self.assertTrue(data["sign_in_required"])
        self.assertFalse(data["claimed"])
        for personal in ("first_name", "email", "mobile", "address_line_1", "postcode"):
            self.assertNotIn(personal, data)
        # Checking doesn't use the link up.
        self.assertIsNone(SwitchContinuationToken.objects.get().used_at)

    def test_claim_needs_sign_in(self):
        token = self.issue()
        resp = self.client.post(self.CLAIM, {"token": token}, format="json")
        self.assertIn(resp.status_code, (401, 403))
        self.assertIsNone(SwitchRequest.objects.get().user)

    def test_claim_links_request_and_uses_the_link(self):
        token = self.issue()
        resp = self.claim(token)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["data"]["reference"], self.sr.reference)
        self.assertEqual(resp.data["data"]["first_name"], "Jane")  # owner view
        sr = SwitchRequest.objects.get()
        self.assertEqual(sr.user, self.user)
        self.assertIsNotNone(sr.claimed_at)
        stored = SwitchContinuationToken.objects.get()
        self.assertEqual(stored.used_by, self.user)
        self.assertTrue(sr.events.filter(kind="claimed", actor=self.user).exists())

        # Same account again (e.g. a double click): fine. Another account: refused.
        self.assertEqual(self.claim(token).status_code, 200)
        other = User.objects.create_user("sam", "sam@example.com", "pw")
        resp = self.claim(token, other)
        self.assertEqual((resp.status_code, resp.data["code"]), (410, "LINK_USED"))
        self.assertEqual(SwitchRequest.objects.get().user, self.user)

    def test_request_owned_by_another_account_is_never_moved(self):
        other = User.objects.create_user("sam", "sam@example.com", "pw")
        SwitchRequest.objects.update(user=other)
        self.sr.refresh_from_db()
        token = self.issue()
        resp = self.claim(token)
        self.assertEqual((resp.status_code, resp.data["code"]), (409, "REQUEST_BELONGS_TO_ANOTHER_ACCOUNT"))
        self.assertEqual(SwitchRequest.objects.get().user, other)
        self.assertIsNone(SwitchContinuationToken.objects.get().used_at)

    def test_expired_link(self):
        token = self.issue()
        SwitchContinuationToken.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        for resp in (self.client.post(self.CHECK, {"token": token}, format="json"), self.claim(token)):
            self.assertEqual((resp.status_code, resp.data["code"]), (410, "LINK_EXPIRED"))

    def test_new_link_replaces_old_one(self):
        old = self.issue()
        new = self.issue()
        resp = self.client.post(self.CHECK, {"token": old}, format="json")
        self.assertEqual((resp.status_code, resp.data["code"]), (404, "INVALID_LINK"))
        self.assertEqual(self.claim(new).status_code, 200)

    def test_unknown_or_tampered_link(self):
        token = self.issue()
        for bad in ("nope", token[:-1] + ("A" if token[-1] != "A" else "B")):
            resp = self.client.post(self.CHECK, {"token": bad}, format="json")
            self.assertEqual((resp.status_code, resp.data["code"]), (404, "INVALID_LINK"))

    def test_request_no_longer_ready(self):
        token = self.issue()
        self.sr.set_status(S.ACTION_NEEDED, reason=SwitchRequest.ActionReason.CODE_EXPIRED)
        resp = self.claim(token)
        self.assertEqual((resp.status_code, resp.data["code"]), (409, "REQUEST_NOT_READY"))
        with self.assertRaises(continuation.ContinuationError):
            continuation.issue_link(self.sr)

    def test_unverified_account_cannot_claim(self):
        token = self.issue()
        self.user.is_active = False
        self.user.save()
        resp = self.claim(token)
        self.assertEqual((resp.status_code, resp.data["code"]), (403, "EMAIL_NOT_VERIFIED"))

    def test_resend_same_reply_whatever_matches(self):
        replies = []
        for ref, email in ((self.sr.reference, "JANE@example.com"), (self.sr.reference, "x@example.com"),
                           ("ZM-SW-AAAAAAAA", "jane@example.com")):
            resp = self.client.post(self.RESEND, {"reference": ref, "email": email}, format="json")
            self.assertEqual(resp.status_code, 200)
            replies.append(resp.data)
        self.assertTrue(replies[0] == replies[1] == replies[2])
        self.assertEqual(len(mail.outbox), 1)  # only the matching one sent
        self.assertEqual(SwitchContinuationToken.objects.count(), 1)

    def test_switched_off(self):
        token = self.issue()
        with override_settings(SWITCH_ORDER_CONVERSION_ENABLED=False):
            for url, client in ((self.CHECK, self.client), (self.CLAIM, None)):
                resp = (client.post(url, {"token": token}, format="json") if client else self.claim(token))
                self.assertEqual((resp.status_code, resp.data["code"]), (404, "NOT_AVAILABLE"))
            resp = self.client.post(self.RESEND, {"reference": self.sr.reference, "email": "jane@example.com"},
                                    format="json")
            self.assertEqual(resp.status_code, 404)
            with self.assertRaises(continuation.ContinuationError):
                continuation.issue_link(self.sr)

    def test_flag_matrix(self):
        """Engineering Decision v2.1 §07: Part B needs conversion AND supplier
        writes, unless Finance approved a time-limited exception."""
        token = self.issue()
        cases = [
            # (conversion, supplier writes, approved exception) → open?
            (False, False, False, False),
            (True, False, False, False),   # would take payment with no activation
            (False, True, False, False),
            (True, True, False, True),
            (True, False, True, True),     # explicit, approved exception only
        ]
        for conversion, supplier, exception, is_open in cases:
            with self.subTest(conversion=conversion, supplier=supplier, exception=exception), override_settings(
                SWITCH_ORDER_CONVERSION_ENABLED=conversion, ADD_LINE_SUPPLIER_WRITES_ENABLED=supplier,
                SWITCH_ORDERS_WITHOUT_SUPPLIER_WRITES_APPROVED=exception,
            ):
                self.assertEqual(continuation.enabled(), is_open)
                resp = self.client.post(self.CHECK, {"token": token}, format="json")
                if is_open:
                    self.assertEqual(resp.status_code, 200, resp.data)
                else:
                    self.assertEqual((resp.status_code, resp.data["code"]), (404, "NOT_AVAILABLE"))
                    with self.assertRaises(continuation.ContinuationError):
                        continuation.issue_link(self.sr)

    def test_admin_says_why_links_are_off(self):
        admin_user = User.objects.create_superuser("ops", "ops@example.com", "pw")
        self.client.force_login(admin_user)
        with override_settings(ADD_LINE_SUPPLIER_WRITES_ENABLED=False):
            resp = self.client.post("/admin/switch/switchrequest/", {
                "action": "send_continuation_link", "_selected_action": [self.sr.pk]}, follow=True)
        self.assertContains(resp, "ADD_LINE_SUPPLIER_WRITES_ENABLED is off")
        self.assertEqual(len(mail.outbox), 0)

    def test_admin_ready_for_order_sends_link_only_when_enabled(self):
        self.sr.set_status(S.VALIDATING)
        admin_user = User.objects.create_superuser("ops", "ops@example.com", "pw")
        self.client.force_login(admin_user)
        with override_settings(SWITCH_ORDER_CONVERSION_ENABLED=False):
            self.client.post("/admin/switch/switchrequest/", {
                "action": "mark_ready_for_order", "_selected_action": [self.sr.pk]})
        self.assertEqual(len(mail.outbox), 0)

        self.sr.set_status(S.VALIDATING)
        self.client.post("/admin/switch/switchrequest/", {
            "action": "mark_ready_for_order", "_selected_action": [self.sr.pk]})
        self.assertEqual(SwitchRequest.objects.get().status, S.READY_FOR_ORDER)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(SwitchContinuationToken.objects.get().created_by, admin_user)


def approval_evidence(**overrides):
    return {
        "approval_reference": "LEGAL-SIGNOFF-2023-014", "approval_date": date(2023, 3, 1),
        "document_owner": "Legal (records custodian)", "source_document": "master.pdf",
        "source_sha256": "a" * 64, "source_location": "Legal controlled repository",
        "effective_from": timezone.localdate(), **overrides,
    }


def draft_terms(version="T-1", term="12M", **overrides):
    fields = {f: f"Approved {f.replace('_', ' ')}." for f in documents._TERMS_FIELDS[6:]}
    terms = ContractTerms.objects.create(version=version, **{**approval_evidence(), **fields, **overrides})
    ContractTermRule.objects.create(terms=terms, term=term, minimum_commitment="12 months from activation.",
                                    renewal="Rolls monthly.", early_termination="Remaining months x price.")
    return terms


def draft_tariff(plan, version="P-1", **overrides):
    fields = {f: f"Approved {f.replace('_', ' ')}." for f in documents._TARIFF_FIELDS}
    return PlanTariff.objects.create(plan=plan, version=version, **{
        **approval_evidence(), **fields, "rollover_applies": False, "price_changes_apply": False, **overrides,
    })


def approved_contract_content(plan, term="12M", version="T-1"):
    """Published Legal terms + Product tariff with approval evidence (test data only)."""
    terms = draft_terms(version, term)
    tariff = draft_tariff(plan)
    for record in (terms, tariff):
        record.record_approval(None)
        record.publish(None)
    return terms, tariff


def pdf_pages(data: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


class ContractRegistryTests(SwitchTestBase):
    """Group 2 (C3–C6): approved-content registry, Contract Recovery & Release v2.3."""

    TERMS_URL = "/api/v1/switch/contract-terms/"

    def plan_url(self, plan_id=None):
        return f"/api/v1/switch/plans/{plan_id or self.plan.pk}/contract/"

    # ── C3: provenance and lifecycle (T01, T09) ──

    def test_only_published_content_is_used(self):
        terms, tariff = draft_terms(), draft_tariff(self.plan)
        self.assertIsNone(documents.find_sources(self.plan.pk, "12M").terms)  # draft
        for record in (terms, tariff):
            record.record_approval(None)
        sources = documents.find_sources(self.plan.pk, "12M")
        self.assertEqual((sources.terms, sources.tariff), (None, None))  # approved, not live
        for record in (terms, tariff):
            record.publish(None)
        sources = documents.find_sources(self.plan.pk, "12M")
        self.assertEqual((sources.terms, sources.tariff), (terms, tariff))
        self.assertEqual(documents.missing_content(sources), [])

    def test_approval_needs_evidence(self):
        terms = draft_terms(approval_reference="", source_sha256="not-a-hash", effective_from=None)
        with self.assertRaises(ValidationError) as ctx:
            terms.record_approval(None)
        text = " ".join(ctx.exception.messages)
        for needed in ("approval_reference", "source_sha256", "effective_from"):
            self.assertIn(needed, text)
        terms.refresh_from_db()
        self.assertEqual(terms.state, "draft")

    def test_approval_needs_every_field(self):
        terms = draft_terms(adr_scheme="  ")
        with self.assertRaises(ValidationError) as ctx:
            terms.record_approval(None)
        self.assertIn("adr_scheme", " ".join(ctx.exception.messages))
        ContractTermRule.objects.filter(terms=terms).update(renewal="")
        self.assertIn("rule 12M.renewal", ContractTerms.objects.get().approval_problems())

    def test_publish_only_after_approval(self):
        terms = draft_terms()
        with self.assertRaises(ValidationError):
            terms.publish(None)

    def test_effective_dates_and_retiring(self):
        today = timezone.localdate()
        old = draft_terms("T-1", effective_from=today - timedelta(days=30))
        new = draft_terms("T-2", effective_from=today + timedelta(days=1))
        for record in (old, new):
            record.record_approval(None)
            record.publish(None)
        self.assertEqual(ContractTerms.current(), old)  # T-2 not in effect yet
        self.assertEqual(ContractTerms.current(today + timedelta(days=1)), new)
        old.retire()
        self.assertIsNone(ContractTerms.current())
        self.assertEqual(old.effective_to, today)
        self.assertEqual(old.state, "retired")

    def test_return_to_draft_only_if_never_published(self):
        terms = draft_terms()
        terms.record_approval(None)
        terms.return_to_draft()
        self.assertEqual((terms.state, terms.approved_at), ("draft", None))
        terms.record_approval(None)
        terms.publish(None)
        with self.assertRaises(ValidationError):
            terms.return_to_draft()

    # ── C4 / C5: tariff versions, rollover and price changes (T02, T03) ──

    def test_tariff_must_answer_rollover_and_price_changes(self):
        tariff = draft_tariff(self.plan, rollover_applies=None, price_changes_apply=None, plan_sku="")
        problems = " ".join(tariff.approval_problems())
        for needed in ("rollover_applies", "price_changes_apply", "plan_sku"):
            self.assertIn(needed, problems)

    def test_formula_price_rises_are_refused(self):
        for wording in ("Rises each April by CPI + 3.9%.", "An annual 4.5 per cent increase applies.",
                        "Linked to the Retail Prices Index."):
            with self.subTest(wording=wording):
                tariff = draft_tariff(self.plan, version=f"P-{abs(hash(wording))}", scheduled_price_changes=wording)
                self.assertTrue(any("CPI/RPI" in p for p in tariff.approval_problems()))

    def test_price_change_rows_must_match_the_answer(self):
        tariff = draft_tariff(self.plan, price_changes_apply=True,
                              scheduled_price_changes="Your monthly price will change as follows:")
        self.assertTrue(any("add the dated" in p for p in tariff.approval_problems()))

        PlanPriceChange.objects.create(tariff=tariff, effective_date=tariff.effective_from, new_price=Decimal("0"))
        problems = " ".join(tariff.approval_problems())
        self.assertIn("more than £0.00", problems)
        self.assertIn("after the tariff's effective_from", problems)

        PlanPriceChange.objects.all().delete()
        PlanPriceChange.objects.create(tariff=tariff, effective_date=tariff.effective_from + timedelta(days=200),
                                       new_price=Decimal("10.50"))
        self.assertEqual(tariff.approval_problems(), [])

        PlanTariff.objects.filter(pk=tariff.pk).update(price_changes_apply=False)
        tariff.refresh_from_db()
        self.assertTrue(any("answer is No" in p or "is No" in p for p in tariff.approval_problems()))

    def test_tariff_versions_over_time(self):
        today = timezone.localdate()
        first = draft_tariff(self.plan, "P-1", effective_from=today - timedelta(days=10))
        second = draft_tariff(self.plan, "P-2", effective_from=today + timedelta(days=5))
        for record in (first, second):
            record.record_approval(None)
            record.publish(None)
        self.assertEqual(PlanTariff.current(self.plan.pk), first)
        self.assertEqual(PlanTariff.current(self.plan.pk, today + timedelta(days=5)), second)

    # ── Admin ──

    def test_admin_needs_the_approval_permission(self):
        terms = draft_terms()
        staff = User.objects.create_user("clerk", "clerk@example.com", "pw", is_staff=True)
        for codename in ("view_contractterms", "change_contractterms"):
            staff.user_permissions.add(Permission.objects.get(codename=codename))
        admin_client = APIClient()
        admin_client.force_login(staff)
        admin_client.post("/admin/switch/contractterms/", {
            "action": "record_approval", "_selected_action": [terms.pk]})
        self.assertEqual(ContractTerms.objects.get().state, "draft")

        staff.user_permissions.add(Permission.objects.get(codename="approve_contractterms"))
        staff = User.objects.get(pk=staff.pk)  # fresh permission cache
        admin_client.force_login(staff)
        admin_client.post("/admin/switch/contractterms/", {
            "action": "record_approval", "_selected_action": [terms.pk]})
        terms.refresh_from_db()
        self.assertEqual((terms.state, terms.approved_by), ("approved", staff))

        # Publishing is a separate permission.
        admin_client.post("/admin/switch/contractterms/", {"action": "publish", "_selected_action": [terms.pk]})
        self.assertEqual(ContractTerms.objects.get().state, "approved")

    def test_admin_shows_what_is_missing_and_freezes_approved_content(self):
        terms = draft_terms(adr_scheme="")
        admin_user = User.objects.create_superuser("ops", "ops@example.com", "pw")
        admin_client = APIClient()
        admin_client.force_login(admin_user)
        resp = admin_client.post("/admin/switch/contractterms/", {
            "action": "record_approval", "_selected_action": [terms.pk]}, follow=True)
        self.assertContains(resp, "adr_scheme")
        self.assertEqual(ContractTerms.objects.get().state, "draft")

        page = admin_client.get(f"/admin/switch/contractterms/{terms.pk}/change/").content.decode()
        self.assertIn('name="adr_scheme"', page)  # draft: editable
        ContractTerms.objects.filter(pk=terms.pk).update(adr_scheme="Approved ADR wording.")
        admin_client.post("/admin/switch/contractterms/", {
            "action": "record_approval", "_selected_action": [terms.pk]})
        page = admin_client.get(f"/admin/switch/contractterms/{terms.pk}/change/").content.decode()
        self.assertNotIn('name="adr_scheme"', page)  # approved: read-only
        self.assertNotIn('name="source_file"', page)

    def test_attaching_the_master_records_its_checksum(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.switch.admin import ApprovedContentForm

        class Form(ApprovedContentForm):
            class Meta:
                model = ContractTerms
                fields = ["version", "source_document", "source_sha256"]

        content = b"%PDF signed master"
        form = Form(data={"version": "T-9"}, files={"source_file": SimpleUploadedFile("Signed CI 2023.pdf", content)})
        self.assertTrue(form.is_valid(), form.errors)
        terms = form.save()
        self.assertEqual(terms.source_document, "Signed CI 2023.pdf")
        self.assertEqual(terms.source_sha256, documents.sha256(content))

    # ── C6: public endpoints ──

    def test_terms_endpoint_only_serves_published_content(self):
        resp = self.client.get(self.TERMS_URL)
        self.assertEqual((resp.status_code, resp.data["code"]), (404, "NOT_AVAILABLE"))
        draft_terms()
        self.assertEqual(self.client.get(self.TERMS_URL).status_code, 404)  # drafts never served

        approved_contract_content(self.plan, version="T-2")
        resp = self.client.get(self.TERMS_URL)
        self.assertEqual(resp.status_code, 200)
        data = resp.data["data"]
        self.assertEqual(data["version"], "T-2")
        self.assertEqual(data["terms"]["adr_scheme"], "Approved adr scheme.")
        self.assertEqual(data["issuer"]["legal_name"], "Zoiko Mobile UK Ltd")
        self.assertIn("12M", data["term_rules"])
        body = resp.content.decode()
        for internal in ("approval_reference", "LEGAL-SIGNOFF", "source_sha256", "source_location", "document_owner"):
            self.assertNotIn(internal, body)

    def test_plan_contract_endpoint(self):
        resp = self.client.get(self.plan_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((resp.data["data"]["contract_ready"], resp.data["data"]["tariff"]), (False, None))

        terms, tariff = approved_contract_content(self.plan)
        data = self.client.get(self.plan_url()).data["data"]
        self.assertTrue(data["contract_ready"])
        self.assertEqual(data["ready_terms"], ["12M"])  # only 12M has an approved term rule
        self.assertEqual(data["tariff"]["version"], "P-1")
        self.assertFalse(data["tariff"]["rollover_applies"])
        self.assertNotIn("source_sha256", str(data))

        self.assertEqual(self.client.get(self.plan_url(999999)).status_code, 404)
        Plan.objects.filter(pk=self.plan.pk).update(is_active=False)
        self.assertEqual(self.client.get(self.plan_url()).data["code"], "PLAN_NOT_FOUND")


@override_settings(SWITCH_ORDER_CONVERSION_ENABLED=True, ADD_LINE_SUPPLIER_WRITES_ENABLED=True,
                   SWITCH_DOCUMENTS_VALID_MINUTES=60, BACKEND_URL="https://api.zoiko.test")
class ContractDocumentsTests(SwitchTestBase):
    """Milestone 4 · B2: order preview and pre-contract documents."""

    def setUp(self):
        super().setUp()
        self.submit()
        self.user = User.objects.create_user("jane", "jane@example.com", "pw")
        SwitchRequest.objects.update(user=self.user)
        self.sr = SwitchRequest.objects.get()
        self.sr.set_status(S.READY_FOR_ORDER)
        self.terms, self.tariff = approved_contract_content(self.plan)
        self.client.force_authenticate(self.user)
        mail.outbox.clear()

    def url(self, suffix):
        return f"/api/switch-requests/{self.sr.reference}/{suffix}"

    def issue(self):
        return self.client.post(self.url("documents/"), format="json")

    def test_preview(self):
        resp = self.client.get(self.url("order/"))
        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.data["data"]
        self.assertEqual(data["quote"]["monthly_price"], "9.00")
        self.assertFalse(data["price_changed"])
        self.assertTrue(data["documents_available"])
        self.assertIsNone(data["documents"])
        self.assertEqual(data["billing_address"]["postcode"], "SW1A 1AA")
        self.assertEqual(data["port_number_masked"], "07*** ***123")

    def test_preview_flags_a_price_change(self):
        Plan.objects.filter(pk=self.plan.pk).update(price_12=Decimal("11.00"))
        data = self.client.get(self.url("order/")).data["data"]
        self.assertTrue(data["price_changed"])
        self.assertEqual(data["submitted_price"]["monthly_price"], "9.00")
        self.assertEqual(data["quote"]["monthly_price"], "11.00")

    def test_owner_only_and_gates(self):
        other = User.objects.create_user("sam", "sam@example.com", "pw")
        self.client.force_authenticate(other)
        for resp in (self.client.get(self.url("order/")), self.issue()):
            self.assertEqual((resp.status_code, resp.data["code"]), (404, "NOT_FOUND"))
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.url("order/")).status_code, (401, 403))

        self.client.force_authenticate(self.user)
        with override_settings(SWITCH_ORDER_CONVERSION_ENABLED=False):
            self.assertEqual(self.issue().data["code"], "NOT_AVAILABLE")
        self.sr.set_status(S.VALIDATING)
        self.assertEqual(self.issue().data["code"], "REQUEST_NOT_READY")

    def test_expired_code_blocks(self):
        self.sr.set_status(S.READY_FOR_ORDER)
        SwitchRequest.objects.update(code_received_at=timezone.now() - timedelta(days=31))
        resp = self.issue()
        self.assertEqual((resp.status_code, resp.data["code"]), (409, "CODE_EXPIRED"))

    def test_issue_documents(self):
        resp = self.issue()
        self.assertEqual(resp.status_code, 201, resp.data)
        data = resp.data["data"]
        self.assertTrue(data["contract_reference"].startswith("ZM-CQ-"))
        self.assertEqual(data["email_status"], "sent")
        self.assertEqual([d["kind"] for d in data["documents"]], ["contract_summary", "contract_information"])

        pack = SwitchContractPack.objects.get()
        for doc in pack.documents.all():
            pdf = bytes(doc.pdf)
            self.assertTrue(pdf.startswith(b"%PDF"))
            self.assertEqual(doc.sha256, documents.sha256(pdf))
        summary = pack.documents.get(kind="contract_summary")
        self.assertEqual(pdf_pages(bytes(summary.pdf)), 1)  # CS-03

        # Frozen content: from the quote and approved records, never the code.
        content_text = str(pack.content)
        self.assertIn("Zoiko Mobile UK Ltd", content_text)
        self.assertIn("16564980", content_text)
        self.assertNotIn("ABC123456", content_text)
        self.assertEqual(pack.content["price"]["core"], "£9.00 per month")
        self.assertRegex(pack.content["references"]["issued_at"], r" (GMT|BST)$")  # UK time
        self.assertEqual((pack.terms_version, pack.tariff_version), ("T-1", "P-1"))

        # Durable copy by email with both PDFs.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(len(mail.outbox[0].attachments), 2)
        self.assertNotIn("ABC123456", mail.outbox[0].subject + mail.outbox[0].body)

    def test_dated_price_rises_printed_in_pounds_and_evidence_recorded(self):
        """T03/T05: rises in £/p with dates for this plan length only; the pack
        records which approved versions it used."""
        today = timezone.localdate()
        rises = draft_tariff(self.plan, "P-2", price_changes_apply=True,
                             scheduled_price_changes="Your monthly price will change on these dates:")
        PlanPriceChange.objects.create(tariff=rises, term="12M", effective_date=date(today.year + 1, 4, 1),
                                       new_price=Decimal("9.50"))
        PlanPriceChange.objects.create(tariff=rises, term="24M", effective_date=date(today.year + 1, 4, 1),
                                       new_price=Decimal("8.40"))
        rises.record_approval(None)
        rises.publish(None)  # newer publish wins on the same effective date

        self.assertEqual(self.issue().status_code, 201)
        content = SwitchContractPack.objects.get().content
        self.assertEqual(content["tariff_version"], "P-2")
        self.assertEqual([r["new_price"] for r in content["price_changes"]], ["9.50"])  # 12M only
        self.assertIn(f"from 1 April {today.year + 1}: £9.50 per month", content["price"]["scheduled_changes"])
        self.assertNotIn("8.40", str(content["price"]))
        self.assertEqual(content["provenance"]["tariff"]["approval_reference"], "LEGAL-SIGNOFF-2023-014")
        self.assertEqual(content["provenance"]["terms"]["source_sha256"], "a" * 64)

    def test_same_documents_until_something_changes(self):
        first = self.issue().data["data"]
        again = self.issue().data["data"]
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(SwitchContractPack.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

        # New approved Legal version → a fresh set; the old one is superseded, not changed.
        old_summary = SwitchContractDocument.objects.get(kind="contract_summary")
        old_bytes = bytes(old_summary.pdf)
        approved_contract_content(make_plan(name="Other", transatelID="X"), version="T-2")
        newer = self.issue().data["data"]
        self.assertNotEqual(newer["id"], first["id"])
        old = SwitchContractPack.objects.get(public_id=first["id"])
        self.assertIsNotNone(old.superseded_at)
        old_summary.refresh_from_db()
        self.assertEqual(bytes(old_summary.pdf), old_bytes)  # CS-10
        self.assertEqual(self.client.get(self.url("documents/")).data["data"]["id"], newer["id"])

    def test_expired_documents_are_replaced(self):
        first = self.issue().data["data"]
        SwitchContractPack.objects.update(offer_valid_until=timezone.now() - timedelta(minutes=1))
        self.assertIsNone(self.client.get(self.url("documents/")).data["data"])
        self.assertNotEqual(self.issue().data["data"]["id"], first["id"])

    def test_preview_reports_missing_content_without_side_effects(self):
        ContractTerms.objects.filter(pk=self.terms.pk).update(adr_scheme="  ")
        self.assertFalse(self.client.get(self.url("order/")).data["data"]["documents_available"])
        self.assertEqual(SwitchRequest.objects.get().status, S.READY_FOR_ORDER)
        self.assertEqual(len(mail.outbox), 0)

    def test_missing_content_parks_request_and_alerts_ops(self):
        """Engineering Decision v2.1 §04: no documents or payment; request
        saved as "Action needed — internal"; fault logged; Ops alerted."""
        ContractTerms.objects.filter(pk=self.terms.pk).update(adr_scheme="  ")
        resp = self.issue()
        self.assertEqual((resp.status_code, resp.data["code"]), (503, "DOCUMENTS_UNAVAILABLE"))
        self.assertEqual(resp.data["message"], ordering.DOCUMENTS_FAILED_MESSAGE)
        self.assertNotIn("adr", resp.data["message"])  # internal reason never shown
        self.assertFalse(SwitchContractPack.objects.exists())

        sr = SwitchRequest.objects.get()
        self.assertEqual((sr.status, sr.action_reason, sr.action_owner),
                         (S.ACTION_NEEDED, "contract_docs", "operations"))
        self.assertIn("terms.adr_scheme", sr.ops_note)
        self.assertEqual(services.next_action(sr), "none")  # nothing for the customer to do
        self.assertFalse(sr.needs_customer)

        self.assertEqual(len(mail.outbox), 1)
        alert = mail.outbox[0]
        self.assertIn(sr.reference, alert.subject)
        self.assertIn("terms.adr_scheme", alert.alternatives[0][0])
        self.assertNotIn("jane@example.com", alert.alternatives[0][0])

        # Trying again while parked: same safe message, no second alert.
        for again in (self.issue(), self.client.get(self.url("order/"))):
            self.assertEqual((again.status_code, again.data["code"]), (503, "DOCUMENTS_UNAVAILABLE"))
        self.assertEqual(len(mail.outbox), 1)

    def test_unpublished_tariff_parks_request(self):
        PlanTariff.objects.update(state="approved")
        self.assertEqual(self.issue().status_code, 503)
        self.assertIn("published PlanTariff", SwitchRequest.objects.get().ops_note)

    def test_summary_must_fit_one_page(self):
        PlanTariff.objects.update(fair_use="Very long fair use wording. " * 400)
        resp = self.issue()
        self.assertEqual((resp.status_code, resp.data["code"]), (503, "DOCUMENTS_UNAVAILABLE"))
        self.assertFalse(SwitchContractPack.objects.exists())
        sr = SwitchRequest.objects.get()
        self.assertEqual(sr.action_reason, "contract_docs")
        self.assertIn("could not be rendered", sr.ops_note)

    def test_ops_reopen_only_once_content_is_fixed(self):
        ContractTerms.objects.filter(pk=self.terms.pk).update(adr_scheme="")
        self.issue()
        admin_user = User.objects.create_superuser("ops", "ops@example.com", "pw")
        admin_client = APIClient()
        admin_client.force_login(admin_user)
        reopen = {"action": "mark_ready_for_order", "_selected_action": [self.sr.pk]}

        resp = admin_client.post("/admin/switch/switchrequest/", reopen, follow=True)
        self.assertContains(resp, "kept on hold")
        self.assertEqual(SwitchRequest.objects.get().status, S.ACTION_NEEDED)

        ContractTerms.objects.filter(pk=self.terms.pk).update(adr_scheme="Approved ADR wording.")
        mail.outbox.clear()
        admin_client.post("/admin/switch/switchrequest/", reopen)
        self.assertEqual(SwitchRequest.objects.get().status, S.READY_FOR_ORDER)
        self.assertEqual(len(mail.outbox), 1)  # new continuation link: "we'll contact you"
        self.assertEqual(self.issue().status_code, 201)

    def test_download(self):
        doc_data = self.issue().data["data"]["documents"][0]
        resp = self.client.get(doc_data["download_url"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("attachment;", resp["Content-Disposition"])
        self.assertIn("no-store", resp["Cache-Control"])
        self.assertEqual(documents.sha256(resp.content), doc_data["sha256"])  # CS-09

        other = User.objects.create_user("sam", "sam@example.com", "pw")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(doc_data["download_url"]).status_code, 404)  # CS-11

    def test_tampered_document_is_not_served(self):
        doc_data = self.issue().data["data"]["documents"][0]
        SwitchContractDocument.objects.filter(public_id=doc_data["id"]).update(pdf=b"%PDF-tampered")
        resp = self.client.get(doc_data["download_url"])
        self.assertEqual((resp.status_code, resp.data["code"]), (500, "DOCUMENT_UNAVAILABLE"))

    def test_documents_are_immutable(self):
        self.issue()
        doc = SwitchContractDocument.objects.first()
        doc.filename = "changed.pdf"
        with self.assertRaises(ImmutableRecord):
            doc.save()
        with self.assertRaises(ImmutableRecord):
            doc.delete()

    def test_email_failure_is_recorded_not_hidden(self):
        with mock.patch("apps.switch.emails.EmailMultiAlternatives.send", side_effect=RuntimeError("smtp")):
            data = self.issue().data["data"]
        self.assertEqual(data["email_status"], "failed")


CHECKOUT_SETTINGS = dict(
    SWITCH_ORDER_CONVERSION_ENABLED=True, SWITCH_DOCUMENTS_VALID_MINUTES=60,
    STRIPE_SECRET_KEY="sk_test_x", MAX_CONSUMER_LINES=5, LINE_HOLD_MINUTES=30, ADD_LINE_REAUTH_MINUTES=60,
    ADD_LINE_CONTRACT_VERSION="v1", ADD_LINE_CONTRACT_SUMMARY_VERSION="s1",
    ADD_LINE_SUPPLIER_WRITES_ENABLED=True,
)


def fake_transatel():
    service = mock.Mock()
    service.activate_order_sim.return_value = {
        "msisdn": "447700900001", "post_status": "Active", "transaction_id": "tx-1",
        "contact": {"status": "success"},
    }
    service.get_subscriber_by_serial.return_value = {"status": "Active"}
    service.request_uk_port_in.return_value = {"transactionId": "tx-port", "transactionStatus": "PENDING"}
    return service


def fake_paid_intent(order, **charge):
    """Stripe PaymentIntent as retrieved with expand=["latest_charge"]."""
    return {
        "id": order.stripe_payment_intent_id, "status": "succeeded",
        "metadata": {"add_line_order": order.order_reference, "source": "zoiko_add_line"},
        "latest_charge": {
            "id": "ch_123", "captured": True, "amount_captured": int(order.total_gross * 100),
            "currency": "gbp", "created": 1763280000,
            "payment_method_details": {"type": "card", "card": {"brand": "visa", "last4": "4242"}},
            **charge,
        },
    }


@override_settings(**CHECKOUT_SETTINGS)
class CheckoutTestBase(SwitchTestBase):
    """Shared set-up for B3/B4: an owned, ready request with approved content."""

    def setUp(self):
        super().setUp()
        self.submit()
        self.user = User.objects.create_user("jane", "jane@example.com", "pw")
        self.user.last_login = timezone.now()  # add_line asks for a recent login
        self.user.save()
        SwitchRequest.objects.update(user=self.user)
        self.sr = SwitchRequest.objects.get()
        self.sr.set_status(S.READY_FOR_ORDER)
        approved_contract_content(self.plan)
        Sim.objects.create(iccid="8944000000000000001", msisdn="447700900001",
                           provisioning_status="Available", type_of_sim="eUICC")
        self.client.force_authenticate(self.user)

        create = mock.patch("apps.add_line.services.checkout.stripe.PaymentIntent.create",
                            side_effect=lambda **kw: SimpleNamespace(id="pi_123", client_secret="pi_123_secret"))
        self.create_intent = create.start()
        self.addCleanup(create.stop)
        for target in ("deliver_qr", "deliver_details"):
            p = mock.patch(f"apps.add_line.services.fulfilment.{target}")
            p.start()
            self.addCleanup(p.stop)

    def url(self, suffix):
        return f"/api/switch-requests/{self.sr.reference}/{suffix}"

    def issue(self):
        return self.client.post(self.url("documents/"), format="json").data["data"]

    def consent(self, pack, **overrides):
        body = {"pack_id": pack["id"], "accepted": True,
                "documents": [{"id": d["id"], "sha256": d["sha256"]} for d in pack["documents"]], **overrides}
        return self.client.post(self.url("documents/consent/"), body, format="json")

    def checkout(self):
        return self.client.post(self.url("checkout/"), format="json")

    def pay(self, order, **charge):
        intent = {"id": order.stripe_payment_intent_id, "status": "succeeded",
                  "amount": int(order.total_gross * 100), "amount_received": int(order.total_gross * 100),
                  "currency": "gbp", "livemode": False,
                  "metadata": {"add_line_order": order.order_reference, "source": "zoiko_add_line"}}
        service = fake_transatel()
        with mock.patch("apps.add_line.services.fulfilment.get_service", return_value=service), \
                mock.patch("apps.add_line.services.porting.get_service", return_value=service), \
                mock.patch("apps.switch.receipts.stripe.PaymentIntent.retrieve",
                           return_value=fake_paid_intent(order, **charge)):
            payments.handle_payment_succeeded(order, intent)
        return service

    def ready_to_pay(self):
        pack = self.issue()
        self.assertEqual(self.consent(pack).status_code, 200)
        resp = self.checkout()
        self.assertEqual(resp.status_code, 201, resp.data)
        return pack, resp.data["data"]


class ConsentAndCheckoutTests(CheckoutTestBase):
    """Milestone 4 · B3: consent, Stripe checkout via add_line, conversion on payment."""

    # ── consent ──

    def test_consent_records_exact_documents(self):
        pack = self.issue()
        resp = self.consent(pack)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["data"]["consented"])
        consent = SwitchContractConsent.objects.get()
        self.assertEqual(consent.documents, {d["id"]: d["sha256"] for d in pack["documents"]})
        self.assertIn("I confirm that I have received", consent.statement)
        self.assertEqual(consent.ip_address, "127.0.0.1")
        self.assertTrue(self.sr.events.filter(kind="consented").exists())
        self.assertEqual(self.consent(pack).status_code, 200)  # harmless repeat
        self.assertEqual(SwitchContractConsent.objects.count(), 1)

    def test_provision_is_recorded_and_copied_into_consent(self):
        """C9 / T04: documents provided (account + email, downloads logged)
        before express consent; opening a PDF isn't required."""
        pack = self.issue()
        events = list(SwitchContractProvision.objects.values_list("method", "succeeded", "document"))
        self.assertEqual(events, [("account", True, None), ("email", True, None)])
        self.assertEqual(SwitchContractProvision.objects.get(method="email").recipient_masked, "j***@example.com")

        self.client.get(pack["documents"][0]["download_url"])
        download = SwitchContractProvision.objects.get(method="download")
        self.assertEqual(str(download.document.public_id), pack["documents"][0]["id"])
        self.assertEqual(download.actor, self.user)

        self.assertEqual(self.consent(pack).status_code, 200)
        provision = SwitchContractConsent.objects.get().provision
        self.assertEqual([p["method"] for p in provision], ["account", "email", "download"])
        self.assertTrue(all(p["succeeded"] for p in provision))

    def test_failed_email_is_recorded_and_account_copy_still_counts(self):
        with mock.patch("apps.switch.emails.EmailMultiAlternatives.send", side_effect=RuntimeError("smtp")):
            pack = self.issue()
        self.assertFalse(SwitchContractProvision.objects.get(method="email").succeeded)
        self.assertEqual(self.consent(pack).status_code, 200)
        self.assertEqual([p["method"] for p in SwitchContractConsent.objects.get().provision], ["account"])

    def test_no_consent_without_provision_evidence(self):
        pack = self.issue()
        SwitchContractProvision.objects.all().delete()  # simulate missing evidence
        resp = self.consent(pack)
        self.assertEqual((resp.status_code, resp.data["code"]), (409, "DOCUMENTS_NOT_PROVIDED"))
        self.assertFalse(SwitchContractConsent.objects.exists())

    def test_provision_evidence_is_immutable(self):
        self.issue()
        event = SwitchContractProvision.objects.first()
        with self.assertRaises(ImmutableRecord):
            event.save()
        with self.assertRaises(ImmutableRecord):
            event.delete()

    def test_consent_must_match_issued_documents(self):
        pack = self.issue()
        wrong = [{"id": d["id"], "sha256": "0" * 64} for d in pack["documents"]]
        for docs in (wrong, [pack["documents"][0]]):
            resp = self.consent(pack, documents=[{"id": d["id"], "sha256": d["sha256"]} for d in docs])
            self.assertEqual((resp.status_code, resp.data["code"]), (409, "DOCUMENTS_MISMATCH"))
        resp = self.consent(pack, accepted=False)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("accepted", resp.data)
        self.assertFalse(SwitchContractConsent.objects.exists())

    def test_consent_to_expired_or_replaced_documents(self):
        pack = self.issue()
        SwitchContractPack.objects.update(offer_valid_until=timezone.now() - timedelta(minutes=1))
        resp = self.consent(pack)
        self.assertEqual((resp.status_code, resp.data["code"]), (409, "DOCUMENTS_EXPIRED"))

    def test_consent_evidence_is_immutable(self):
        self.consent(self.issue())
        consent = SwitchContractConsent.objects.get()
        consent.statement = "changed"
        with self.assertRaises(ImmutableRecord):
            consent.save()

    # ── checkout ──

    def test_checkout_needs_consent(self):
        self.issue()
        resp = self.checkout()
        self.assertEqual((resp.status_code, resp.data["code"]), (409, "CONSENT_REQUIRED"))
        self.assertFalse(AddLineOrder.objects.exists())

    def test_checkout_creates_add_line_order(self):
        pack, data = self.ready_to_pay()
        self.assertEqual(data["client_secret"], "pi_123_secret")
        self.assertTrue(data["payment_required"])
        self.assertEqual(data["amount"], "9.00")
        order = AddLineOrder.objects.get(order_reference=data["order_reference"])
        self.assertEqual(data["confirm_url"], f"/api/v1/lines/orders/{order.order_reference}/confirm/")
        self.assertEqual(order.owner, self.user)
        self.assertEqual(order.switch_choice, "pac")
        self.assertEqual(order.billing["street"], "10 Example Street")
        self.assertEqual(order.billing["zip_code"], "SW1A 1AA")
        self.assertIsNone(order.delivery_address)  # eSIM
        metadata = self.create_intent.call_args.kwargs["metadata"]
        self.assertEqual(metadata["switch_request"], self.sr.reference)
        self.assertEqual(metadata["source"], "zoiko_add_line")

        sr = SwitchRequest.objects.get()
        self.assertEqual(sr.order, order)
        self.assertEqual(sr.status, S.READY_FOR_ORDER)  # not converted until paid
        self.assertEqual(SwitchContractConsent.objects.get().order, order)
        self.assertFalse(services.tracker(sr)["steps"][1]["done"])

    def test_checkout_again_resumes_the_same_payment(self):
        _, first = self.ready_to_pay()
        with mock.patch("apps.switch.ordering.stripe.PaymentIntent.retrieve",
                        return_value={"id": "pi_123", "client_secret": "pi_123_secret"}):
            resp = self.checkout()
        self.assertEqual(resp.data["data"]["order_reference"], first["order_reference"])
        self.assertEqual(AddLineOrder.objects.count(), 1)

    def test_price_change_after_consent_blocks_checkout(self):
        self.consent(self.issue())
        Plan.objects.filter(pk=self.plan.pk).update(price_12=Decimal("11.00"))
        resp = self.checkout()
        self.assertEqual((resp.status_code, resp.data["code"]), (409, "PRICE_CHANGED"))
        self.assertFalse(AddLineOrder.objects.exists())

    def test_checkout_refused_while_supplier_writes_off(self):
        """C2: never take payment for a line that can't be activated."""
        self.consent(self.issue())
        with override_settings(ADD_LINE_SUPPLIER_WRITES_ENABLED=False):
            resp = self.checkout()
        self.assertEqual((resp.status_code, resp.data["code"]), (404, "NOT_AVAILABLE"))
        self.create_intent.assert_not_called()
        self.assertFalse(AddLineOrder.objects.exists())
        self.assertEqual(SwitchRequest.objects.get().status, S.READY_FOR_ORDER)  # request preserved

        with override_settings(ADD_LINE_SUPPLIER_WRITES_ENABLED=False,
                               SWITCH_ORDERS_WITHOUT_SUPPLIER_WRITES_APPROVED=True):
            self.assertEqual(self.checkout().status_code, 201)  # approved exception only

    def test_expired_code_is_never_sent_to_transatel(self):
        """C11: submit re-checks the code's 30-day life, held work included."""
        from apps.add_line.services import porting

        line = Line.objects.create(owner=self.user, sim=Sim.objects.get(), plan=self.plan, term="12M",
                                   status=Line.Status.ACTIVE)
        port = PortRequest.objects.create(
            line=line, code_type="PAC", encrypted_code=pac.encrypt("ABC123456"),
            portability_msisdn="447700900123", code_expires_at=timezone.localdate(),
        )
        service = fake_transatel()
        with mock.patch("apps.add_line.services.porting.get_service", return_value=service):
            porting.submit(port)
        port.refresh_from_db()
        self.assertEqual(port.status, PortRequest.Status.ERROR)
        self.assertEqual(port.last_error["detail"], porting.CODE_EXPIRED_DETAIL)
        service.request_uk_port_in.assert_not_called()

    def test_transfer_refuses_a_code_already_past_its_life(self):
        from apps.add_line.services import porting

        line = Line.objects.create(owner=self.user, sim=Sim.objects.get(), plan=self.plan, term="12M",
                                   status=Line.Status.PENDING_ACTIVATION)
        with self.assertRaises(porting.PortError) as ctx:
            porting.create_request(self.user, line, code_type="PAC", code="ABC123456", msisdn="07700900123",
                                   code_received_at=timezone.now() - timedelta(days=31))
        self.assertEqual(ctx.exception.code, "CODE_EXPIRED")
        self.assertFalse(PortRequest.objects.exists())

    def test_stripe_failure_places_no_order(self):
        self.consent(self.issue())
        self.create_intent.side_effect = RuntimeError("stripe down")
        resp = self.checkout()
        self.assertEqual((resp.status_code, resp.data["code"]), (502, "PAYMENT_INIT_FAILED"))
        self.assertIsNone(SwitchRequest.objects.get().order)

    # ── conversion on payment ──

    def test_payment_converts_and_hands_code_to_transfer(self):
        _, data = self.ready_to_pay()
        order = AddLineOrder.objects.get(order_reference=data["order_reference"])
        service = self.pay(order)

        sr = SwitchRequest.objects.get()
        self.assertEqual(sr.status, S.CONVERTED)
        self.assertIsNotNone(sr.converted_at)
        self.assertEqual(sr.line, Line.objects.get())
        port = PortRequest.objects.get()
        self.assertEqual((port.code_type, port.portability_msisdn), ("PAC", "447700900123"))
        self.assertEqual(pac.decrypt(port.encrypted_code), "ABC123456")
        # The code's 30-day life runs from when the request received it (C11).
        self.assertEqual(port.code_expires_at, timezone.localdate(sr.code_received_at) + timedelta(days=30))
        self.assertEqual(sr.encrypted_code, "")  # held in one place only
        service.request_uk_port_in.assert_called_once()

        steps = {s["key"]: s["done"] for s in services.tracker(sr)["steps"]}
        self.assertEqual(steps, {"request_received": True, "order_placed": True, "sim_ready": True,
                                 "switch_scheduled": False, "number_transferred": False})
        self.assertEqual(services.pac_status(sr), "submitted")
        self.assertTrue(sr.events.filter(kind="code_transferred").exists())
        self.assertEqual(services.customer_status(sr)[1], "Order created")

        resp = self.checkout()
        self.assertEqual((resp.status_code, resp.data["code"]), (409, "ALREADY_ORDERED"))

    def test_code_later_converts_without_a_transfer(self):
        SwitchRequest.objects.update(encrypted_code="", pac_later=True)
        _, data = self.ready_to_pay()
        self.pay(AddLineOrder.objects.get(order_reference=data["order_reference"]))
        self.assertEqual(SwitchRequest.objects.get().status, S.CONVERTED)
        self.assertFalse(PortRequest.objects.exists())

    def test_payment_after_ops_moved_request_is_honoured_and_flagged(self):
        _, data = self.ready_to_pay()
        self.sr.refresh_from_db()
        self.sr.set_status(S.CANCELLED)
        self.pay(AddLineOrder.objects.get(order_reference=data["order_reference"]))
        sr = SwitchRequest.objects.get()
        self.assertEqual(sr.status, S.CONVERTED)
        self.assertIn("paid while the request was 'cancelled'", sr.ops_note)

    def test_free_order_converts_without_stripe(self):
        Coupon.objects.create(name="FREE", slug="free", discount=Decimal("9.00"))
        SwitchRequest.objects.update(promo_code="free")
        self.sr.refresh_from_db()
        pack = self.issue()
        self.consent(pack)
        with mock.patch("apps.add_line.services.fulfilment.get_service", return_value=fake_transatel()), \
                mock.patch("apps.add_line.services.porting.get_service", return_value=fake_transatel()):
            resp = self.checkout()
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertFalse(resp.data["data"]["payment_required"])
        self.create_intent.assert_not_called()
        self.assertEqual(SwitchRequest.objects.get().status, S.CONVERTED)

    def test_signals_do_not_touch_plain_add_line_orders(self):
        order = AddLineOrder.objects.create(
            owner=self.user, plan=self.plan, term="12M", sim_type="esim", switch_choice="none",
            total_gross=Decimal("9.00"), vat_amount=Decimal("1.50"), stripe_payment_intent_id="pi_x",
        )
        self.pay(order)
        self.assertEqual(SwitchRequest.objects.get().status, S.READY_FOR_ORDER)


class ReceiptTests(CheckoutTestBase):
    """Milestone 4 · B4: payment record, receipt, "Your documents" card."""

    def paid_order(self, **charge):
        _, data = self.ready_to_pay()
        order = AddLineOrder.objects.get(order_reference=data["order_reference"])
        mail.outbox.clear()
        self.pay(order, **charge)
        return order

    def card(self):
        resp = self.client.get(self.url("order-documents/"))
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.data["data"]

    def test_confirmed_payment_gets_one_receipt(self):
        order = self.paid_order()
        capture = SwitchPaymentCapture.objects.get()
        self.assertEqual((capture.order, capture.amount_minor, capture.currency), (order, 900, "GBP"))
        self.assertEqual(capture.method_label, "Visa ending 4242")
        self.assertEqual(capture.receipt_status, "ready")
        receipt = capture.receipt
        self.assertTrue(receipt.receipt_number.startswith("ZM-RC-"))
        pdf = bytes(receipt.pdf)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(pdf_pages(pdf), 1)
        self.assertEqual(receipt.sha256, documents.sha256(pdf))

        receipt_mail = [m for m in mail.outbox if receipt.receipt_number in m.subject]
        self.assertEqual(len(receipt_mail), 1)
        self.assertEqual(receipt_mail[0].attachments[0][0], receipt.filename)
        self.assertTrue(receipt.deliveries.get().accepted)

        # Replays (webhook + confirm, or the daily job) never add a second one (PR-07).
        receipts.record_payment(order)
        with mock.patch("apps.switch.receipts.stripe.PaymentIntent.retrieve",
                        return_value=fake_paid_intent(order)):
            receipts.reconcile()
        self.assertEqual(SwitchPaymentCapture.objects.count(), 1)
        self.assertEqual(SwitchPaymentReceipt.objects.count(), 1)

    def test_receipt_has_no_vat_details(self):
        order = self.paid_order()
        content = receipts.receipt_content(SwitchPaymentCapture.objects.get(), "ZM-RC-TEST")
        self.assertEqual(content["total"], "£9.00")
        self.assertNotIn("vat", " ".join(content).lower())
        self.assertNotIn("ch_123", str(content))  # safe public reference only
        self.assertTrue(content["transaction_reference"].startswith("TX-"))
        self.assertEqual(content["order_reference"], order.order_reference)

    def test_documents_card(self):
        self.paid_order()
        data = self.card()
        self.assertEqual(data["payment"]["state"], "paid")
        self.assertEqual([d["kind"] for d in data["documents"]],
                         ["contract_summary", "contract_information", "payment_receipt"])
        receipt_row = data["documents"][2]
        self.assertEqual(receipt_row["email_status"], "sent")
        resp = self.client.get(receipt_row["download_url"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(documents.sha256(resp.content), receipt_row["sha256"])
        # The contract documents in the card are the ones agreed to.
        consent = SwitchContractConsent.objects.get()
        self.assertEqual({d["sha256"] for d in data["documents"][:2]}, set(consent.documents.values()))

        other = User.objects.create_user("sam", "sam@example.com", "pw")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(receipt_row["download_url"]).status_code, 404)

    def test_pending_payment_has_no_receipt(self):
        self.ready_to_pay()
        data = self.card()
        self.assertEqual(data["payment"]["state"], "pending")
        self.assertNotIn("payment_receipt", [d["kind"] for d in data["documents"]])

    def test_amount_mismatch_withholds_receipt(self):
        self.paid_order(amount_captured=100)
        self.assertFalse(SwitchPaymentCapture.objects.exists())
        self.assertIn("Receipt withheld", SwitchRequest.objects.get().ops_note)
        self.assertEqual(self.card()["payment"]["state"], "preparing")

    def test_free_order_has_no_receipt(self):
        Coupon.objects.create(name="FREE", slug="free", discount=Decimal("9.00"))
        SwitchRequest.objects.update(promo_code="free")
        self.consent(self.issue())
        with mock.patch("apps.add_line.services.fulfilment.get_service", return_value=fake_transatel()), \
                mock.patch("apps.add_line.services.porting.get_service", return_value=fake_transatel()):
            self.checkout()
        self.assertFalse(SwitchPaymentCapture.objects.exists())
        self.assertEqual(self.card()["payment"],
                         {"state": "no_payment", "message": "No payment was taken for this order."})

    def test_promo_line_items_add_up(self):
        Coupon.objects.create(name="HALF", slug="half", discount=Decimal("4.50"))
        SwitchRequest.objects.update(promo_code="half")
        self.paid_order()
        content = receipts.receipt_content(SwitchPaymentCapture.objects.get(), "ZM-RC-TEST")
        self.assertEqual([i[2] for i in content["items"]], ["£9.00", "−£4.50"])
        self.assertEqual(content["total"], "£4.50")

    def test_receipt_failure_is_retried(self):
        with mock.patch("apps.switch.documents.render_receipt", side_effect=RuntimeError("pdf")):
            self.paid_order()
        capture = SwitchPaymentCapture.objects.get()
        self.assertEqual((capture.receipt_status, capture.receipt_attempts), ("failed", 1))
        self.assertEqual(self.card()["payment"]["state"], "preparing")

        out = StringIO()
        call_command("reconcile_switch_receipts", stdout=out)
        capture.refresh_from_db()
        self.assertEqual(capture.receipt_status, "ready")
        self.assertIn("receipts issued: 1", out.getvalue())

    def _webhook(self, event):
        with mock.patch("apps.add_line.views.stripe.Webhook.construct_event", return_value=event):
            return APIClient().post("/api/v1/lines/webhooks/stripe/", b"{}", content_type="application/json",
                                    HTTP_STRIPE_SIGNATURE="t=1,v1=x")

    @override_settings(STRIPE_ADD_LINE_WEBHOOK_SECRET="whsec_" + "a1" * 16)
    def test_refund_and_dispute_are_linked_records(self):
        self.paid_order()
        receipt = SwitchPaymentReceipt.objects.get()
        original = bytes(receipt.pdf)
        refund = {"id": "evt_r1", "type": "charge.refunded", "livemode": False, "created": 1763290000,
                  "data": {"object": {"id": "ch_123", "amount_refunded": 300, "status": "succeeded"}}}
        self.assertEqual(self._webhook(refund).status_code, 200)
        self._webhook(refund)  # replayed
        capture = SwitchPaymentCapture.objects.get()
        self.assertEqual((capture.status, capture.refunded_minor), ("partially_refunded", 300))
        self.assertEqual(capture.adjustments.count(), 1)
        receipt.refresh_from_db()
        self.assertEqual(bytes(receipt.pdf), original)  # PR-16
        self.assertEqual(self.card()["payment"]["message"], "A refund of £3.00 has been issued.")

        dispute = {"id": "evt_d1", "type": "charge.dispute.created", "livemode": False, "created": 1763300000,
                   "data": {"object": {"id": "dp_1", "charge": "ch_123", "amount": 600,
                                       "status": "needs_response"}}}
        self._webhook(dispute)
        self.assertEqual(self.card()["payment"]["state"], "disputed")
        self.assertIn("payment_receipt", [d["kind"] for d in self.card()["documents"]])

    def test_staff_resend_is_logged(self):
        self.paid_order()
        admin_user = User.objects.create_superuser("ops", "ops@example.com", "pw")
        self.client.force_login(admin_user)
        self.client.post("/admin/switch/switchpaymentcapture/", {
            "action": "resend_receipt", "_selected_action": [SwitchPaymentCapture.objects.get().pk]})
        resend = SwitchDocumentDelivery.objects.get(resend=True)
        self.assertEqual((resend.actor, resend.accepted), (admin_user, True))
        self.assertEqual(resend.recipient_masked, "j***@example.com")

    def test_receipts_are_immutable(self):
        self.paid_order()
        receipt = SwitchPaymentReceipt.objects.get()
        with self.assertRaises(ImmutableRecord):
            receipt.save()
        with self.assertRaises(ImmutableRecord):
            receipt.delete()


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
