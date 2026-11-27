"""CTO Addendum v2.1 A1 / acceptance test 1: existing purchase emails continue,
but CT-005 credentials never pass through the legacy email jobs."""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from apps.sim_change.services.suppression import is_change_case_sim
from apps.sim_change.services.transitions import open_case
from apps.sims import esim, psim
from apps.sims.models import Sim

from .helpers import TEST_KEYS, make_line, make_sim, make_user

QR = {"qr_code_value": "LPA:1$smdp.example$MATCH", "qr_code_data_url": "", "activation_code": "LPA:1$x"}


@TEST_KEYS
class SuppressionTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.line = make_line(self.user)  # physical SIM
        self.case = open_case(owner=self.user, line=self.line, to_type="esim")
        self.new_sim = make_sim(esim=True)
        self.case.new_sim = self.new_sim
        self.case.save(update_fields=["new_sim"])

    def test_both_case_sims_are_suppressed_and_others_are_not(self):
        self.assertTrue(is_change_case_sim(self.new_sim))
        self.assertTrue(is_change_case_sim(self.line.sim))
        self.assertFalse(is_change_case_sim(make_sim(esim=True)))

    def test_suppression_survives_the_case_ending(self):
        self.case.status = "CANCELLED"
        self.case.save(update_fields=["status"])
        self.assertTrue(is_change_case_sim(self.new_sim))

    def test_change_esim_never_fetched_stored_or_emailed(self):
        service = mock.Mock()
        with mock.patch("apps.sims.esim.send_esim_qr_email") as send:
            self.assertEqual(esim.deliver_qr(service, self.new_sim), "suppressed")
        service.get_esim_qr.assert_not_called()
        send.assert_not_called()
        self.new_sim.refresh_from_db()
        self.assertEqual((self.new_sim.esim_qr_value, self.new_sim.esim_activation_code), ("", ""))

    def test_change_psim_details_never_emailed(self):
        with mock.patch("apps.sims.psim.send_psim_details_email") as send:
            self.assertEqual(psim.deliver_details(self.line.sim), "suppressed")
        send.assert_not_called()

    def test_purchase_esim_still_delivered(self):
        ordinary = make_sim(esim=True)
        service = mock.Mock(get_esim_qr=mock.Mock(return_value=QR))
        with mock.patch("apps.sims.esim.customer_for", return_value=("buyer@example.com", "Buyer")), \
                mock.patch("apps.sims.esim.send_esim_qr_email", return_value=True) as send:
            self.assertEqual(esim.deliver_qr(service, ordinary), "sent")
        send.assert_called_once()

    def test_cron_jobs_skip_change_sims(self):
        Sim.objects.filter(pk__in=[self.new_sim.pk, self.line.sim.pk]).update(inventory=Sim.Inventory.ACTIVATED)
        out = StringIO()
        call_command("send_esim_qr", "--dry-run", stdout=out)
        self.assertNotIn(self.new_sim.iccid, out.getvalue())
        out = StringIO()
        call_command("send_psim_details", "--dry-run", stdout=out)
        self.assertNotIn(self.line.sim.iccid, out.getvalue())

    def test_cron_jobs_still_pick_up_purchase_sims(self):
        ordinary = make_sim(esim=True)
        Sim.objects.filter(pk=ordinary.pk).update(inventory=Sim.Inventory.ACTIVATED)
        out = StringIO()
        call_command("send_esim_qr", "--dry-run", stdout=out)
        self.assertIn(ordinary.iccid, out.getvalue())
