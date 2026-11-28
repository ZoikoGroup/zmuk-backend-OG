"""Customer alerts and the "This wasn't me" link (CTO v2.0 §10.1, Addendum v2.1 §4.4;
acceptance test 12)."""

import re
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from apps.security.models import SuspiciousReport
from apps.sim_change.models import SimTypeChangeEvent
from apps.sim_change.services import notifications, operations
from apps.sim_change.services.transitions import transition

from .helpers import POLL, S, make_evidence, make_sim, to_swap_in_progress
from .test_esim_access import EsimTestBase

REPORT_URL = "/api/v1/sim-type-changes/report-unauthorised-link/"


@override_settings(FRONTEND_URL="https://react.example.test")
class AlertTests(EsimTestBase):
    def new_case_with_alerts(self, **kw):
        with self.captureOnCommitCallbacks(execute=True):
            return self.new_case(**kw)

    def notices(self, case):
        return list(SimTypeChangeEvent.objects.filter(case=case, kind__startswith="notification_")
                    .values_list("kind", "detail__notice"))

    def test_received_alert_has_masked_number_and_report_link(self):
        case = self.new_case_with_alerts()
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, [self.customer.email])
        self.assertEqual(email.subject, "We've received your SIM change request")
        html = email.alternatives[0][0]
        self.assertIn("•••• ••0123", html)
        self.assertNotIn("447700900123", html)
        self.assertNotIn(case.old_sim.iccid, html)
        self.assertIn("https://react.example.test/sim-change/report?token=", html)
        self.assertIn("This wasn't me", html)
        self.assertEqual(self.notices(case), [("notification_sent", "received")])
        event = SimTypeChangeEvent.objects.get(case=case, kind="notification_sent")
        self.assertEqual(event.detail["to"], "c***@example.com")

    def test_pre_swap_and_post_swap_alerts(self):
        case = self.new_case_with_alerts()
        self.verify(case)
        case.refresh_from_db()
        from apps.sim_change.services import requests as req
        case = req.confirm_request(self.customer, case, {"quote_hash": case.quote_hash})
        case.new_sim = make_sim(esim=True)
        case.save(update_fields=["new_sim"])
        case = transition(case, S.STOCK_RESERVED, source="support")
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            case = to_swap_in_progress(case, self.helper_staff)
        self.assertEqual([m.subject for m in mail.outbox], ["Your SIM change is about to be processed"])
        self.assertIn("You may temporarily lose mobile service", mail.outbox[0].alternatives[0][0])
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            transition(case, S.SUPPLIER_BINDING_CONFIRMED, source=POLL, evidence=make_evidence(case))
        self.assertEqual([m.subject for m in mail.outbox], ["Your Zoiko Mobile service has moved to your new SIM"])
        self.assertNotIn(case.new_sim.iccid, mail.outbox[0].alternatives[0][0])

    def test_paused_alert_has_no_report_link(self):
        case = self.new_case_with_alerts()
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            transition(case, S.HOLD_FRAUD, source="customer")
        self.assertEqual(mail.outbox[0].subject, "We've paused your SIM change request")
        self.assertNotIn("This wasn't me", mail.outbox[0].alternatives[0][0])

    def test_email_failure_is_recorded_and_does_not_undo_the_change(self):
        with mock.patch("apps.sim_change.services.notifications.EmailMultiAlternatives.send",
                        side_effect=RuntimeError("smtp down")):
            case = self.new_case_with_alerts()
        case.refresh_from_db()
        self.assertEqual(case.status, S.VERIFY_REQUIRED)
        self.assertEqual(self.notices(case), [("notification_failed", "received")])

    def test_no_email_on_account(self):
        self.customer.email = ""
        self.customer.save()
        case = self.new_case_with_alerts()
        self.assertEqual(mail.outbox, [])
        event = SimTypeChangeEvent.objects.get(case=case, kind="notification_failed")
        self.assertEqual(event.detail["reason"], "no email")

    def test_alert_only_after_commit(self):
        self.new_case()  # no on-commit execution
        self.assertEqual(mail.outbox, [])


@override_settings(FRONTEND_URL="https://react.example.test")
class ReportLinkTests(EsimTestBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.case = self.new_case()
        self.anon = APIClient()  # no login: the session may be the compromised one

    def token(self, case=None):
        return re.search(r"token=([^&\"]+)", notifications.report_url(case or self.case)).group(1)

    def test_link_reports_without_login(self):
        from urllib.parse import unquote
        response = self.anon.post(REPORT_URL, {"token": unquote(self.token())}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(set(response.data), {"status", "message"})  # nothing about the case
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.HOLD_FRAUD)
        self.assertTrue(SuspiciousReport.objects.filter(user=self.customer, note__contains="email link").exists())

    def test_invalid_or_tampered_token(self):
        response = self.anon.post(REPORT_URL, {"token": "nonsense"}, format="json")
        self.assertEqual((response.status_code, response.data["code"]), (400, "INVALID_LINK"))
        forged = notifications.signing.dumps({"c": str(self.case.uuid), "u": self.customer.pk + 999},
                                             salt=notifications.SALT, compress=True)
        response = self.anon.post(REPORT_URL, {"token": forged}, format="json")
        self.assertEqual(response.data["code"], "INVALID_LINK")
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.VERIFY_REQUIRED)

    def test_expired_link(self):
        token = notifications.make_report_token(self.case)
        with override_settings(SIM_CHANGE_REPORT_LINK_DAYS=-1):
            response = self.anon.post(REPORT_URL, {"token": token}, format="json")
        self.assertEqual((response.status_code, response.data["code"]), (410, "LINK_EXPIRED"))

    def test_report_after_completed_swap_still_opens_incident(self):
        case = operations.staff_cancel(self.case, actor=self.agent, reason="customer called")  # terminal case
        response = self.anon.post(REPORT_URL, {"token": notifications.make_report_token(case)}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertTrue(SuspiciousReport.objects.filter(user=self.customer).exists())
