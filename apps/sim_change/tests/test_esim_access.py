"""Secure eSIM setup (CTO Addendum v2.1 A2; acceptance tests 6, 7, 13 and the
eSIM rows of the v2.0 UAT matrix)."""

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.sim_change.models import SimTypeChangeEvent, StepUpGrant
from apps.sim_change.services import esim_access
from apps.sim_change.services import requests as req
from apps.sim_change.services.transitions import TransitionError, transition
from apps.sims.transatel import TransatelNotConfigured

from .helpers import POLL, S, Staff, make_evidence, make_sim, make_user, to_swap_in_progress
from .test_operations import OpsTestBase

PROFILE = {
    "qr_code_value": "LPA:1$smdp.example.net$MATCH-123", "smdp_address": "smdp.example.net",
    "matching_id": "MATCH-123", "qr_code_data_url": "data:image/png;base64,AAAA", "activation_code": "x",
    "status": "RELEASED", "status_date": "",
}
SECRET = "MATCH-123"
ON = override_settings(SIM_CHANGE_ESIM_ACCESS_ENABLED=True)


def supplier_profile(profile=PROFILE, error=None):
    service = mock.Mock()
    if error:
        service.get_esim_qr.side_effect = error
    else:
        service.get_esim_qr.return_value = profile
    return mock.patch("apps.sim_change.services.supplier.get_service", return_value=service)


class EsimTestBase(OpsTestBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        # Separate people for the swap steps (OpsTestBase already uses the names agent/authoriser/executor).
        self.helper_staff = Staff.__new__(Staff)
        self.helper_staff.agent = self.agent
        self.helper_staff.authoriser = make_user("ops-auth", staff=True)
        self.helper_staff.executor = make_user("ops-exec", staff=True)
        self.api = APIClient()
        self.api.force_authenticate(self.customer)

    def bound_case(self):
        """An eSIM case whose swap the supplier has confirmed."""
        case = self.new_case(to_type="esim")
        self.verify(case)
        case.refresh_from_db()
        case = req.confirm_request(self.customer, case, {"quote_hash": case.quote_hash})
        case.new_sim = make_sim(esim=True)
        case.save(update_fields=["new_sim"])
        case = transition(case, S.STOCK_RESERVED, source="support")
        case = to_swap_in_progress(case, self.helper_staff)
        return transition(case, S.SUPPLIER_BINDING_CONFIRMED, source=POLL, evidence=make_evidence(case))

    def issue(self, case, agent=None, **kw):
        with supplier_profile():
            return esim_access.issue_access(case, agent=agent or self.agent, policy_version="SEC-VER-1", **kw)

    def open_url(self, case):
        return f"/api/v1/sim-type-changes/{case.uuid}/activation-access/"


class IssueAccessTests(EsimTestBase):
    def test_not_before_supplier_confirms_the_swap(self):
        case = self.new_case(to_type="esim")
        with self.assertRaises(TransitionError) as ctx:
            self.issue(case)
        self.assertEqual(ctx.exception.code, "BINDING_NOT_CONFIRMED")

    def test_wrong_role_refused(self):
        with self.assertRaises(TransitionError) as ctx:
            self.issue(self.bound_case(), agent=self.executor)
        self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")

    def test_profile_not_ready(self):
        case = self.bound_case()
        with supplier_profile(profile={"qr_code_value": ""}):
            with self.assertRaises(TransitionError) as ctx:
                esim_access.issue_access(case, agent=self.agent, policy_version="SEC-VER-1")
        self.assertEqual(ctx.exception.code, "PROFILE_NOT_READY")

    def test_issue_makes_access_ready_and_service_ready_without_storing_anything(self):
        case = self.bound_case()
        grant = self.issue(case)
        case.refresh_from_db()
        self.assertEqual((case.status, case.esim_fulfilment), (S.SERVICE_READY, "ACCESS_READY"))
        self.assertEqual(grant.purpose, StepUpGrant.Purpose.ESIM_INSTALL_ACCESS)
        case.new_sim.refresh_from_db()
        self.assertEqual((case.new_sim.esim_qr_value, case.new_sim.esim_activation_code), ("", ""))
        self.assertFalse(case.new_sim.esim_qr_image)
        self.assertNotIn(SECRET, str(list(case.events.values_list("detail", flat=True))))


class OpenAccessTests(EsimTestBase):
    def setUp(self):
        super().setUp()
        self.case = self.bound_case()
        self.issue(self.case)

    def open(self, profile=PROFILE, error=None):
        with supplier_profile(profile, error):
            return self.api.post(self.open_url(self.case), {}, format="json")

    def test_flag_off_refuses(self):
        response = self.open()
        self.assertEqual((response.status_code, response.data["code"]), (403, "FEATURE_DISABLED"))

    @ON
    def test_returns_details_once_with_safe_headers(self):
        response = self.open()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["activation_code"], PROFILE["qr_code_value"])
        self.assertEqual(response.data["qr_image"], PROFILE["qr_code_data_url"])
        self.assertTrue(response.data["one_time"])
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.case.refresh_from_db()
        self.assertEqual(self.case.esim_fulfilment, "ACCESS_ISSUED")
        # Nothing kept anywhere.
        self.case.new_sim.refresh_from_db()
        self.assertEqual(self.case.new_sim.esim_qr_value, "")
        self.assertNotIn(SECRET, str(list(SimTypeChangeEvent.objects.values_list("detail", flat=True))))
        # Second open is refused: single use.
        again = self.open()
        self.assertEqual((again.status_code, again.data["code"]), (409, "ACCESS_NOT_AVAILABLE"))

    @ON
    def test_someone_else_cannot_open_it(self):
        other = APIClient()
        other.force_authenticate(make_user("mallory"))
        with supplier_profile():
            self.assertEqual(other.post(self.open_url(self.case), {}, format="json").status_code, 404)

    @ON
    def test_expired_access_refused_and_can_be_reissued(self):
        StepUpGrant.objects.filter(case=self.case).update(expires_at=timezone.now() - timedelta(minutes=1))
        response = self.open()
        self.assertEqual((response.status_code, response.data["code"]), (412, "ACCESS_EXPIRED"))
        self.issue(self.case)  # agent re-issues after a fresh check
        self.assertEqual(self.open().status_code, 200)

    @ON
    @override_settings(SIM_CHANGE_ESIM_PILOT_USER_IDS="999999")
    def test_pilot_allowlist(self):
        self.assertEqual(self.open().data["code"], "FEATURE_DISABLED")

    @ON
    def test_supplier_down_keeps_access_unused(self):
        response = self.open(error=TransatelNotConfigured("x"))
        self.assertEqual((response.status_code, response.data["code"]), (503, "SUPPLIER_UNAVAILABLE"))
        self.assertIsNotNone(esim_access.valid_access_grant(self.case))
        self.assertEqual(self.open().status_code, 200)

    @ON
    def test_detail_offers_setup_then_report_installed(self):
        detail = self.api.get(f"/api/v1/sim-type-changes/{self.case.uuid}/").data
        self.assertIn("esim_setup", detail["allowed_actions"])
        self.assertTrue(detail["message"].startswith("Connect your phone to Wi-Fi"))
        self.open()
        detail = self.api.get(f"/api/v1/sim-type-changes/{self.case.uuid}/").data
        self.assertNotIn("esim_setup", detail["allowed_actions"])
        self.assertIn("report_installed", detail["allowed_actions"])
        response = self.api.post(f"/api/v1/sim-type-changes/{self.case.uuid}/installed/", {}, format="json")
        self.assertEqual((response.status_code, response.data["esim_fulfilment"]), (202, "INSTALLATION_REPORTED"))

    @ON
    def test_staff_verify_installation(self):
        self.open()
        self.case.refresh_from_db()
        case = esim_access.verify_installation(self.case, actor=self.executor, note="test call OK")
        self.assertEqual(case.esim_fulfilment, "INSTALLATION_VERIFIED")


class RecoveryTests(EsimTestBase):
    def test_exception_withdraws_access_and_reissue_needs_supplier_operation(self):
        case = self.bound_case()
        self.issue(case)
        case.refresh_from_db()
        case = esim_access.record_exception(case, actor=self.executor, reason="profile consumed on old phone")
        self.assertEqual(case.esim_fulfilment, "EXCEPTION")
        self.assertIsNone(esim_access.valid_access_grant(case))
        with self.assertRaises(TransitionError) as ctx:
            self.issue(case)
        self.assertEqual(ctx.exception.code, "SUPPLIER_OPERATION_REQUIRED")
        self.issue(case, supplier_operation_reference="AUR-NEW-ESIM-7")
        case.refresh_from_db()
        self.assertEqual(case.esim_fulfilment, "ACCESS_READY")

    def test_cannot_issue_while_valid_access_exists(self):
        case = self.bound_case()
        self.issue(case)
        case.refresh_from_db()
        with self.assertRaises(TransitionError) as ctx:
            self.issue(case)
        self.assertEqual(ctx.exception.code, "ACCESS_ALREADY_READY")

    def test_physical_case_rejected(self):
        with self.assertRaises(TransitionError) as ctx:
            self.issue(self.new_case(to_type="psim"))
        self.assertEqual(ctx.exception.code, "NOT_ESIM")
