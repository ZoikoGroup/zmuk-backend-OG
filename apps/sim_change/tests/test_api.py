"""Customer API (CTO v2.0 §8 / §8.1, Addendum v2.1 acceptance tests 3, 9, 12, 14)."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import UserProfile
from apps.add_line.models import Line
from apps.plans.models import Plan
from apps.security.models import SuspiciousReport
from apps.sim_change.models import SimChangePlanEligibility, SimTypeChangeCase, StatusSource
from apps.sim_change.services import requests as req
from apps.sim_change.services.supplier import LineRead
from apps.sim_change.services.transitions import set_fulfilment, transition

from .helpers import S, SUP, SYS, TEST_KEYS, Staff, make_grant, make_sim, make_user

GOOD_READ = LineRead(ok=True, supplier_status="Active", active=True, msisdn_matches=True,
                     read_at=timezone.now(), correlation_id="ref-1")
ESIM_DEVICE = {"device_esim_compatible": True, "device_unlocked": True, "wifi_available": True}
ADDRESS = {"line1": "1 High Street", "town": "Leeds", "postcode": "ls11ba"}


def read_patch(read=GOOD_READ):
    return mock.patch("apps.sim_change.services.supplier.read_line", return_value=read)


@TEST_KEYS
class ApiTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.plan = Plan.objects.create(name="Standard 10GB", price=Decimal("10.00"), duration_days=30)
        SimChangePlanEligibility.objects.create(
            plan=self.plan, version="1", enabled=True, psim_to_esim=True, esim_to_psim=True,
            effective_from=timezone.now() - timedelta(days=1),
        )
        self.line = self.make_line()
        patcher = read_patch()
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_line(self, esim=False, owner=None, **extra):
        return Line.objects.create(owner=owner or self.user, sim=make_sim(esim=esim), plan=self.plan,
                                   status=Line.Status.ACTIVE, current_msisdn="447700900123", **extra)

    def url(self, path):
        return f"/api/v1/{path}"

    def quote(self, line=None, target="esim", device=None):
        line = line or self.line
        response = self.client.post(self.url(f"lines/{line.uuid}/sim-type-changes/quote/"),
                                    {"target_type": target, "device": device or ESIM_DEVICE}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def create(self, line=None, target="esim", key="key-000001", **overrides):
        line = line or self.line
        body = self.create_body(line, target, **overrides)
        return self.client.post(self.url(f"lines/{line.uuid}/sim-type-changes/"), body, format="json",
                                HTTP_IDEMPOTENCY_KEY=key)

    def create_body(self, line, target="esim", **overrides):
        q = self.quote(line, target, ESIM_DEVICE if target == "esim" else {"device_takes_physical_sim": True})
        body = {
            "target_type": target,
            "device": ESIM_DEVICE if target == "esim" else {"device_takes_physical_sim": True},
            "quote": {"quote_hash": q["quote_hash"], "expires_at": q["expires_at"]},
            "consent": {"accepted": True, "wording_version": q["consent_wording_version"]},
        }
        if target == "psim":
            body["delivery_address"] = ADDRESS
        body.update(overrides)
        return body


class OptionsTests(ApiTestBase):
    def get(self, line=None):
        return self.client.get(self.url(f"lines/{(line or self.line).uuid}/sim-type-options/"))

    def codes(self, response):
        return [r["code"] for r in response.data["blocked_reasons"]]

    def test_eligible_line_offers_only_the_opposite_type(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["available"])
        self.assertEqual(response.data["current_type"], "psim")
        self.assertEqual(response.data["target"]["type"], "esim")
        self.assertEqual(response.data["target"]["label"], "Request a change to eSIM")
        self.assertEqual(response.data["msisdn_masked"], "•••• ••0123")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_someone_elses_line_is_404(self):
        other = self.make_line(owner=make_user("bob"))
        self.assertEqual(self.get(other).status_code, 404)

    def test_supplier_unavailable_is_503_and_not_actionable(self):
        with read_patch(LineRead(ok=False, correlation_id="ref-x")):
            response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.data["available"])
        self.assertIn("SUPPLIER_STATUS_UNAVAILABLE", self.codes(response))
        self.assertEqual(response.data["correlation_id"], "ref-x")

    def test_supplier_shows_different_number(self):
        with read_patch(LineRead(ok=True, supplier_status="Active", active=True, msisdn_matches=False,
                                 read_at=timezone.now())):
            self.assertIn("SUPPLIER_MISMATCH", self.codes(self.get()))

    def test_plan_without_rule_is_not_eligible(self):
        SimChangePlanEligibility.objects.all().delete()
        self.assertIn("PLAN_NOT_ELIGIBLE", self.codes(self.get()))

    def test_direction_disabled_for_plan(self):
        SimChangePlanEligibility.objects.update(psim_to_esim=False)
        self.assertIn("PLAN_NOT_ELIGIBLE", self.codes(self.get()))

    def test_legacy_claimed_line_needs_flag(self):
        line = self.make_line(source=Line.Source.LEGACY_CLAIM)
        self.assertIn("LEGACY_LINE_REVIEW", self.codes(self.get(line)))
        with override_settings(SIM_CHANGE_LEGACY_CLAIMS_ENABLED=True):
            self.assertTrue(self.get(line).data["available"])

    def test_inactive_line(self):
        Line.objects.filter(pk=self.line.pk).update(status=Line.Status.SUSPENDED)
        self.assertIn("LINE_NOT_ACTIVE", self.codes(self.get()))

    @override_settings(SIM_TYPE_CHANGE_MODE="off")
    def test_journey_switched_off(self):
        self.assertEqual(self.codes(self.get()), ["FEATURE_DISABLED"])

    def test_account_under_review_after_report(self):
        SuspiciousReport.objects.create(user=self.user, note="x")
        self.assertIn("ACCOUNT_UNDER_REVIEW", self.codes(self.get()))

    def test_requires_login(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.get().status_code, 401)


class QuoteTests(ApiTestBase):
    def test_quote_shows_no_invented_price(self):
        data = self.quote()
        self.assertEqual(data["charge"]["status"], "NOT_SHOWN")
        self.assertIsNone(data["charge"]["amount"])
        self.assertEqual(data["charge"]["message"], "Any applicable charge will be shown before you confirm.")
        self.assertTrue(data["quote_hash"])
        self.assertFalse(SimTypeChangeCase.objects.exists())  # read-only

    def test_device_not_ready(self):
        response = self.client.post(self.url(f"lines/{self.line.uuid}/sim-type-changes/quote/"),
                                    {"target_type": "esim", "device": {"device_esim_compatible": True}},
                                    format="json")
        self.assertEqual((response.status_code, response.data["code"]), (422, "DEVICE_NOT_READY"))

    def test_same_type_target_rejected(self):
        response = self.client.post(self.url(f"lines/{self.line.uuid}/sim-type-changes/quote/"),
                                    {"target_type": "psim", "device": {}}, format="json")
        self.assertEqual(response.data["code"], "INVALID_TARGET")


class CreateTests(ApiTestBase):
    def test_creates_support_managed_request(self):
        response = self.create()
        self.assertEqual(response.status_code, 202, response.data)
        case = SimTypeChangeCase.objects.get(uuid=response.data["case_id"])
        self.assertEqual(case.status, S.VERIFY_REQUIRED)
        self.assertEqual(response.data["action_owner"], "support")
        self.assertEqual(case.consent["wording_version"], "2026-11-27")
        self.assertEqual(case.quote["charge"]["status"], "NOT_SHOWN")
        self.assertIn("report_unauthorised", response.data["allowed_actions"])
        self.assertIn("cancel", response.data["allowed_actions"])
        self.assertFalse(case.commands.exists())  # nothing sent to the supplier

    def test_idempotency_key_required(self):
        response = self.create(key="")
        self.assertEqual((response.status_code, response.data["code"]), (422, "IDEMPOTENCY_KEY_REQUIRED"))

    def test_replay_returns_same_case(self):
        body = self.create_body(self.line)
        url = self.url(f"lines/{self.line.uuid}/sim-type-changes/")
        first = self.client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="same-key-1")
        second = self.client.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="same-key-1")
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.data["case_id"], first.data["case_id"])
        self.assertEqual(SimTypeChangeCase.objects.count(), 1)

    def test_key_reused_with_different_body(self):
        self.create(key="same-key-2")
        response = self.client.post(self.url(f"lines/{self.line.uuid}/sim-type-changes/"), {"target_type": "esim"},
                                    format="json", HTTP_IDEMPOTENCY_KEY="same-key-2")
        self.assertEqual((response.status_code, response.data["code"]), (422, "IDEMPOTENCY_KEY_REUSED"))

    def test_failed_request_can_retry_with_same_key(self):
        bad = self.create(key="retry-key-1", consent={"accepted": False})
        self.assertEqual(bad.data["code"], "CONSENT_REQUIRED")
        self.assertEqual(self.create(key="retry-key-1").status_code, 202)

    def test_tampered_or_expired_quote(self):
        response = self.create(quote={"quote_hash": "0" * 64, "expires_at": self.quote()["expires_at"]})
        self.assertEqual((response.status_code, response.data["code"]), (422, "QUOTE_CHANGED"))
        past = (timezone.now() - timedelta(minutes=1)).replace(microsecond=0).isoformat()
        response = self.create(key="key-000002", quote={"quote_hash": "x", "expires_at": past})
        self.assertEqual((response.status_code, response.data["code"]), (412, "QUOTE_EXPIRED"))

    def test_outdated_consent_wording(self):
        response = self.create(consent={"accepted": True, "wording_version": "old"})
        self.assertEqual((response.status_code, response.data["code"]), (412, "CONSENT_OUTDATED"))

    def test_open_request_blocks_second(self):
        self.create()
        # Even a quote is refused once a request is open.
        response = self.client.post(self.url(f"lines/{self.line.uuid}/sim-type-changes/quote/"),
                                    {"target_type": "esim", "device": ESIM_DEVICE}, format="json")
        self.assertEqual((response.status_code, response.data["code"]), (409, "OPEN_REQUEST"))

    def test_recent_password_change_marks_high_risk(self):
        self.user.set_password("new-password-9")
        self.user.save()
        case = SimTypeChangeCase.objects.get(uuid=self.create().data["case_id"])
        self.assertIn("RECENT_PASSWORD_CHANGE", case.risk_flags)
        opened = case.events.get(status_after=S.ELIGIBILITY_PENDING)
        self.assertEqual(opened.detail["risk_tier"], "high")

    def test_physical_request_needs_valid_address(self):
        line = self.make_line(esim=True)
        response = self.create(line=line, target="psim", delivery_address={"line1": "", "postcode": "x"})
        self.assertEqual((response.status_code, response.data["code"]), (422, "INVALID_ADDRESS"))
        self.assertIn("postcode", response.data["errors"])

    def test_physical_request_new_address_flagged_on_file_not(self):
        line = self.make_line(esim=True)
        case = SimTypeChangeCase.objects.get(uuid=self.create(line=line, target="psim").data["case_id"])
        self.assertEqual(case.delivery_address_source, "new")
        self.assertIn("DELIVERY_ADDRESS_REVIEW", case.risk_flags)
        self.assertNotIn("High Street", str(self.client.get(self.url(f"sim-type-changes/{case.uuid}/")).data))

    def test_address_matching_profile_postcode_is_on_file(self):
        UserProfile.objects.filter(user=self.user).update(delivery_address="1 High Street, Leeds LS1 1BA")
        line = self.make_line(esim=True)
        case = SimTypeChangeCase.objects.get(uuid=self.create(line=line, target="psim").data["case_id"])
        self.assertEqual(case.delivery_address_source, "on_file")


class CaseActionTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.staff = Staff()
        self.case = SimTypeChangeCase.objects.get(uuid=self.create().data["case_id"])

    def test_detail_own_case_only(self):
        response = self.client.get(self.url(f"sim-type-changes/{self.case.uuid}/"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["ETag"], f'"{self.case.uuid}-v{self.case.version}"')
        self.assertNotIn("447700900123", str(response.data))
        other = APIClient()
        other.force_authenticate(make_user("mallory"))
        self.assertEqual(other.get(self.url(f"sim-type-changes/{self.case.uuid}/")).status_code, 404)

    def test_list(self):
        response = self.client.get(self.url("sim-type-changes/"))
        self.assertEqual([c["case_id"] for c in response.data["cases"]], [str(self.case.uuid)])

    def test_cancel(self):
        response = self.client.post(self.url(f"sim-type-changes/{self.case.uuid}/cancel/"), {}, format="json")
        self.assertEqual((response.status_code, response.data["status"]), (202, S.CANCELLED))
        self.assertNotIn("cancel", response.data["allowed_actions"])

    def test_cancel_with_stale_version(self):
        response = self.client.post(self.url(f"sim-type-changes/{self.case.uuid}/cancel/"),
                                    {"expected_version": self.case.version + 5}, format="json")
        self.assertEqual((response.status_code, response.data["code"]), (409, "CASE_CHANGED"))

    def _to_awaiting_confirmation(self):
        case = transition(self.case, S.VERIFIED, source=SUP, step_up=make_grant(self.case, self.staff.agent))
        return req.requote_after_verification(case, actor=self.staff.agent)

    def test_confirm_after_verification(self):
        case = self._to_awaiting_confirmation()
        self.assertEqual(case.status, S.AWAITING_CONFIRMATION)
        detail = self.client.get(self.url(f"sim-type-changes/{case.uuid}/")).data
        self.assertIn("confirm", detail["allowed_actions"])
        response = self.client.post(self.url(f"sim-type-changes/{case.uuid}/confirm/"),
                                    {"quote": {"quote_hash": case.quote_hash}}, format="json")
        self.assertEqual((response.status_code, response.data["status"]), (202, S.CASE_ACCEPTED))

    def test_confirm_with_wrong_quote(self):
        case = self._to_awaiting_confirmation()
        response = self.client.post(self.url(f"sim-type-changes/{case.uuid}/confirm/"),
                                    {"quote": {"quote_hash": "nope"}}, format="json")
        self.assertEqual((response.status_code, response.data["code"]), (422, "QUOTE_CHANGED"))

    def test_report_unauthorised_opens_incident_and_holds(self):
        response = self.client.post(self.url(f"sim-type-changes/{self.case.uuid}/report-unauthorised/"),
                                    {"note": "not me"}, format="json")
        self.assertEqual((response.status_code, response.data["status"]), (202, S.HOLD_FRAUD))
        self.assertTrue(response.data["message"].startswith("We've paused this request"))
        report = SuspiciousReport.objects.get(user=self.user)
        self.assertIn(str(self.case.uuid), report.note)
        options = self.client.get(self.url(f"lines/{self.line.uuid}/sim-type-options/"))
        self.assertIn("ACCOUNT_UNDER_REVIEW", [r["code"] for r in options.data["blocked_reasons"]])


class ReceiptTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.staff = Staff()
        line = self.make_line(esim=True)
        case = SimTypeChangeCase.objects.get(uuid=self.create(line=line, target="psim").data["case_id"])
        case = transition(case, S.VERIFIED, source=SUP, step_up=make_grant(case, self.staff.agent))
        case = req.requote_after_verification(case)
        case = transition(case, S.CASE_ACCEPTED, source=StatusSource.CUSTOMER)
        case.new_sim = make_sim()
        case.save(update_fields=["new_sim"])
        case = transition(case, S.STOCK_RESERVED, source=SUP)
        case = set_fulfilment(case, "physical", "AWAITING_DISPATCH", source=SUP)
        self.case = set_fulfilment(case, "physical", "SHIPPED", source=SUP, carrier="DPD", tracking_reference="T1")

    def post(self, iccid):
        return self.client.post(self.url(f"sim-type-changes/{self.case.uuid}/confirm-receipt/"),
                                {"iccid": iccid}, format="json")

    def test_shipped_case_shows_tracking_and_receipt_action(self):
        data = self.client.get(self.url(f"sim-type-changes/{self.case.uuid}/")).data
        self.assertEqual(data["delivery"]["tracking_reference"], "T1")
        self.assertIn("confirm_receipt", data["allowed_actions"])
        self.assertEqual(data["new_sim_last4"], self.case.new_sim.iccid[-4:])

    def test_correct_iccid_confirms_receipt(self):
        response = self.post(" ".join([self.case.new_sim.iccid[:10], self.case.new_sim.iccid[10:]]))
        self.assertEqual((response.status_code, response.data["physical_fulfilment"]), (202, "RECEIPT_CONFIRMED"))

    def test_wrong_iccid_attempts_are_limited(self):
        for _ in range(4):
            self.assertEqual(self.post("89440000000000099999").data["code"], "ICCID_MISMATCH")
        self.assertEqual(self.post("89440000000000099999").data["code"], "ICCID_MISMATCH")
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.SUPPORT_REQUIRED)
        response = self.post(self.case.new_sim.iccid)
        self.assertEqual((response.status_code, response.data["code"]), (429, "RECEIPT_ATTEMPTS_EXCEEDED"))
