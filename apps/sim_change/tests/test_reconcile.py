"""Supplier reconciliation and cutover (CTO v2.0 §9, Addendum v2.1 §4.2;
acceptance tests 5, 10, 11 and the SIMCARD/SWAPPED event)."""

import hashlib
import json
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.add_line import webhooks
from apps.add_line.models import SupplierEvent
from apps.sim_change.models import ProviderCommand, SimTypeChangeCase, SwapSimStock
from apps.sim_change.services import reconcile
from apps.sim_change.services.transitions import TransitionError, open_case, set_fulfilment
from apps.sims.models import Sim
from apps.sims.transatel import TransatelAPIError, TransatelNotConfigured

from .helpers import (
    CUST, S, SUP, TEST_KEYS, Staff, deliver_physical, make_line, make_user, to_stock_reserved, to_swap_in_progress,
)

NUMBER = "447700900123"
NOT_FOUND = TransatelAPIError("not found", status_code=404)


class FakeService:
    def __init__(self, reads):
        self.reads = reads

    def get_subscriber_by_serial(self, serial):
        result = self.reads.get(serial, {})
        if isinstance(result, Exception):
            raise result
        return result


@TEST_KEYS
class ReconcileTestBase(TestCase):
    def setUp(self):
        self.user = make_user()
        self.staff = Staff()

    def swapped_case(self, to_type="esim"):
        line = make_line(self.user, esim=to_type == "psim", msisdn=NUMBER)
        line.sim.msisdn = NUMBER
        line.sim.save(update_fields=["msisdn"])
        case = to_stock_reserved(open_case(owner=self.user, line=line, to_type=to_type), self.staff)
        Sim.objects.filter(pk=case.new_sim_id).update(inventory_purpose="swap", inventory=Sim.Inventory.RESERVED)
        SwapSimStock.objects.create(sim=case.new_sim, sim_format=to_type, source="t", status="RESERVED",
                                    reserved_case=case)
        if to_type == "psim":
            case = deliver_physical(case, self.staff)
        return to_swap_in_progress(case, self.staff)

    def supplier(self, case, new=None, old=None):
        reads = {case.new_sim.iccid: new if new is not None else {"status": "Active", "msisdn": NUMBER},
                 case.old_sim.iccid: old if old is not None else NOT_FOUND}
        return mock.patch("apps.sim_change.services.supplier.get_service", return_value=FakeService(reads))

    def overdue(self, case):
        SimTypeChangeCase.objects.filter(pk=case.pk).update(supplier_submitted_at=timezone.now() - timedelta(hours=2))


class CutoverTests(ReconcileTestBase):
    def test_proven_esim_swap_confirms_and_switches_line(self):
        case = self.swapped_case()
        old_sim, new_sim = case.old_sim, case.new_sim
        with self.supplier(case):
            self.assertEqual(reconcile.check_case(case), "confirmed")
        case.refresh_from_db()
        old_sim.refresh_from_db()
        new_sim.refresh_from_db()
        case.line.refresh_from_db()
        self.assertEqual(case.status, S.SUPPLIER_BINDING_CONFIRMED)  # eSIM waits for secure install access
        self.assertEqual(case.line.sim_id, new_sim.pk)
        self.assertEqual((old_sim.inventory, old_sim.msisdn), (Sim.Inventory.RETIRED, ""))
        self.assertEqual((new_sim.inventory, new_sim.msisdn), (Sim.Inventory.ACTIVATED, NUMBER))
        self.assertEqual(SwapSimStock.objects.get(sim=new_sim).status, "ASSIGNED")
        self.assertEqual(case.commands.get().status, ProviderCommand.Status.ACKNOWLEDGED)
        evidence = case.evidence.get()
        self.assertTrue(evidence.binding_confirmed)
        self.assertEqual(evidence.checks["subscriber_basis"], "msisdn_continuity")
        self.assertNotIn(NUMBER, json.dumps(evidence.checks))

    def test_number_lookups_find_the_new_sim(self):
        case = self.swapped_case()
        with self.supplier(case):
            reconcile.check_case(case)
        self.assertEqual(Sim.objects.filter(msisdn=NUMBER).get().pk, case.new_sim_id)

    def test_physical_swap_goes_to_service_ready(self):
        case = self.swapped_case(to_type="psim")
        with self.supplier(case):
            reconcile.check_case(case)
        case.refresh_from_db()
        self.assertEqual(case.status, S.SERVICE_READY)

    def test_subscriber_id_must_match_when_known(self):
        case = self.swapped_case()
        case.before_snapshot = {**case.before_snapshot, "supplier_subscriber_id": "SUB-1"}
        case.save(update_fields=["before_snapshot"])
        with self.supplier(case, new={"status": "Active", "msisdn": NUMBER, "subscriberId": "SUB-2"}):
            self.assertEqual(reconcile.check_case(case), "pending")
        with self.supplier(case, new={"status": "Active", "msisdn": NUMBER, "subscriberId": "SUB-1"}):
            self.assertEqual(reconcile.check_case(case), "confirmed")

    def test_changed_plan_blocks_confirmation(self):
        from decimal import Decimal
        from apps.plans.models import Plan
        case = self.swapped_case()
        case.line.plan = Plan.objects.create(name="Other", price=Decimal("5"), duration_days=30)
        case.line.save(update_fields=["plan"])
        with self.supplier(case):
            self.assertEqual(reconcile.check_case(case), "pending")
        case.refresh_from_db()
        self.assertEqual(case.status, S.SWAP_IN_PROGRESS)


class UnclearOutcomeTests(ReconcileTestBase):
    def test_not_visible_yet_waits_then_goes_to_reconciliation(self):
        case = self.swapped_case()
        not_yet = dict(new={"status": "Available", "msisdn": ""}, old={"status": "Active", "msisdn": NUMBER})
        with self.supplier(case, **not_yet):
            self.assertEqual(reconcile.check_case(case), "pending")
            self.overdue(case)
            self.assertEqual(reconcile.check_case(case), "reconciliation")
        case.refresh_from_db()
        self.assertEqual(case.status, S.RECONCILIATION_REQUIRED)
        self.assertEqual(case.line.sim_id, case.old_sim_id)  # line untouched

    def test_split_brain_goes_to_reconciliation_immediately(self):
        case = self.swapped_case()
        with self.supplier(case, new={"status": "Available", "msisdn": ""}, old={"status": "Terminated"}):
            with self.assertLogs("apps.sim_change", level="CRITICAL"):
                self.assertEqual(reconcile.check_case(case), "split_brain")
        case.refresh_from_db()
        self.assertEqual(case.status, S.RECONCILIATION_REQUIRED)

    def test_supplier_unreadable_is_unknown_never_failed(self):
        case = self.swapped_case()
        with mock.patch("apps.sim_change.services.supplier.get_service",
                        return_value=FakeService({case.new_sim.iccid: TransatelNotConfigured("x")})):
            self.assertEqual(reconcile.check_case(case), "unknown")
            self.overdue(case)
            self.assertEqual(reconcile.check_case(case), "reconciliation")
        case.refresh_from_db()
        self.assertNotEqual(case.status, S.FAILED)
        self.assertFalse(case.evidence.exists())

    def test_reconciliation_case_confirms_when_swap_appears(self):
        case = self.swapped_case()
        with self.supplier(case, new={"status": "Available", "msisdn": ""}, old={"status": "Terminated"}):
            with self.assertLogs("apps.sim_change", level="CRITICAL"):
                reconcile.check_case(case)
        with self.supplier(case):
            self.assertEqual(reconcile.check_case(case), "confirmed")

    def test_close_not_swapped_needs_fresh_proof(self):
        case = self.swapped_case()
        with self.supplier(case, new={"status": "Available", "msisdn": ""}, old={"status": "Active", "msisdn": NUMBER}):
            self.overdue(case)
            reconcile.check_case(case)
        case.refresh_from_db()
        # Supplier now shows the number on the new SIM but old not detached: unclear → refuse.
        with self.supplier(case, new={"status": "Available", "msisdn": NUMBER}, old={"status": "Active", "msisdn": NUMBER}):
            with self.assertRaises(TransitionError) as ctx:
                reconcile.fail_if_not_swapped(case, actor=self.staff.authoriser, note="checked")
            self.assertEqual(ctx.exception.code, "SWAP_OUTCOME_UNCLEAR")
        with self.supplier(case, new={"status": "Available", "msisdn": ""}, old={"status": "Active", "msisdn": NUMBER}):
            case = reconcile.fail_if_not_swapped(case, actor=self.staff.authoriser, note="Auriga shows old SIM active")
        self.assertEqual(case.status, S.FAILED)
        self.assertEqual(case.commands.get().status, ProviderCommand.Status.FAILED)
        self.assertEqual(SwapSimStock.objects.get(sim=case.new_sim).status, "QUARANTINED")

    def test_no_retry_or_new_command_is_created(self):
        case = self.swapped_case()
        with self.supplier(case, new={"status": "Available", "msisdn": ""}, old={"status": "Active", "msisdn": NUMBER}):
            self.overdue(case)
            reconcile.check_case(case)
            reconcile.check_case(case)
        self.assertEqual(case.commands.count(), 1)


class JobAndWebhookTests(ReconcileTestBase):
    def test_command_reconciles_open_cases(self):
        case = self.swapped_case()
        out = StringIO()
        with self.supplier(case):
            call_command("reconcile_sim_type_changes", stdout=out)
        self.assertIn("confirmed=1", out.getvalue())

    def _event(self, event_type, serial):
        payload = {"header": {"eventId": f"e-{event_type}-{serial}", "eventType": event_type},
                   "body": {"simSerial": serial}}
        body = json.dumps(payload).encode()
        return SupplierEvent.objects.create(event_id=payload["header"]["eventId"], event_type=event_type,
                                            payload=payload, payload_sha256=hashlib.sha256(body).hexdigest())

    def test_simcard_swapped_event_triggers_a_fresh_read(self):
        case = self.swapped_case()
        event = self._event(webhooks.SIMCARD_SWAPPED, case.new_sim.iccid)
        with self.supplier(case):
            webhooks.process(event)
        event.refresh_from_db()
        case.refresh_from_db()
        self.assertEqual(event.status, SupplierEvent.Status.PROCESSED)
        self.assertEqual(case.status, S.SUPPLIER_BINDING_CONFIRMED)

    def test_event_alone_never_confirms(self):
        case = self.swapped_case()
        event = self._event(webhooks.SIMCARD_SWAPPED, case.new_sim.iccid)
        with self.supplier(case, new={"status": "Available", "msisdn": ""}, old={"status": "Active", "msisdn": NUMBER}):
            webhooks.process(event)
        case.refresh_from_db()
        self.assertEqual(case.status, S.SWAP_IN_PROGRESS)

    def test_msisdn_swapped_event_is_ignored(self):
        case = self.swapped_case()
        event = self._event("CONNECTIVITY-MANAGEMENT/SUBSCRIBER/MSISDN/SWAPPED", case.new_sim.iccid)
        webhooks.process(event)
        event.refresh_from_db()
        case.refresh_from_db()
        self.assertEqual(event.status, SupplierEvent.Status.IGNORED)
        self.assertEqual(case.status, S.SWAP_IN_PROGRESS)

    def test_event_for_unknown_sim_is_ignored(self):
        event = self._event(webhooks.SIMCARD_SWAPPED, "8944000000009999999")
        webhooks.process(event)
        event.refresh_from_db()
        self.assertEqual(event.status, SupplierEvent.Status.IGNORED)
