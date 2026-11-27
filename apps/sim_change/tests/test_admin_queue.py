"""Admin work queue pages: buttons per role, action forms, audited address view."""

from django.test import Client
from django.urls import reverse

from apps.sim_change.models import SimTypeChangeEvent, SwapSimStock

from .test_operations import OpsTestBase, swap_stock


class AdminQueueTests(OpsTestBase):
    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def case_page(self, case):
        return reverse("admin:sim_change_simtypechangecase_change", args=[case.pk])

    def action_url(self, case, key):
        return reverse("admin:sim_change_case_action", args=[case.uuid, key])

    def test_agent_sees_verification_button_qa_sees_none(self):
        case = self.new_case()
        page = self.client_for(self.agent).get(self.case_page(case))
        self.assertContains(page, "Record identity verification")
        page = self.client_for(self.qa).get(self.case_page(case))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Record identity verification")
        self.assertContains(page, "No step is waiting for your role")

    def test_action_url_forbidden_for_wrong_role(self):
        case = self.new_case()
        self.assertEqual(self.client_for(self.qa).get(self.action_url(case, "verify")).status_code, 403)
        self.assertEqual(self.client_for(self.authoriser).get(self.action_url(case, "verify")).status_code, 403)

    def test_record_verification_through_admin(self):
        case = self.new_case()
        response = self.client_for(self.agent).post(self.action_url(case, "verify"), {
            "method": "supervised_support", "assurance": "passed", "policy_version": "SEC-VER-1",
            "external_reference": "CALL-77", "risk_decision_id": "",
        })
        self.assertRedirects(response, self.case_page(case), fetch_redirect_response=False)
        case.refresh_from_db()
        self.assertEqual(case.status, "AWAITING_CONFIRMATION")

    def test_service_error_shown_on_form(self):
        case = self.to_accepted(self.new_case())  # no stock imported
        response = self.client_for(self.authoriser).post(self.action_url(case, "reserve_stock"),
                                                         {"supplier_check_reference": "AUR-1"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "NO_SWAP_STOCK")

    def test_reserve_then_dispatch_page_shows_address_and_audits_it(self):
        case = self.to_accepted(self.new_case(to_type="psim"))
        swap_stock("psim")
        self.client_for(self.authoriser).post(self.action_url(case, "reserve_stock"), {"supplier_check_reference": "AUR-1"})
        self.assertEqual(SwapSimStock.objects.get().status, "RESERVED")
        page = self.client_for(self.executor).get(self.action_url(case, "dispatch"))
        self.assertContains(page, "LS1 1BA")
        self.assertTrue(SimTypeChangeEvent.objects.filter(case=case, kind="delivery_address_viewed").exists())

    def test_unavailable_step_redirects(self):
        case = self.new_case()
        response = self.client_for(self.authoriser).get(self.action_url(case, "reserve_stock"))
        self.assertRedirects(response, self.case_page(case), fetch_redirect_response=False)

    def test_work_queue_filter(self):
        case = self.new_case()
        response = self.client_for(self.agent).get(
            reverse("admin:sim_change_simtypechangecase_changelist") + "?queue=verify")
        self.assertContains(response, str(case.uuid))
