"""Maker-checker on supplier swaps (CTO Addendum v2.1 A3, acceptance tests 4, 5, 10)."""

from django.test import TestCase, override_settings

from apps.sim_change.models import ProviderCommand, SimTypeChangeEvent
from apps.sim_change.services import commands
from apps.sim_change.services.transitions import TransitionError, open_case

from .helpers import S, TEST_KEYS, Staff, make_line, make_sim, make_user, to_stock_reserved


@TEST_KEYS
class AuthoriseSwapTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.staff = Staff()
        self.case = to_stock_reserved(
            open_case(owner=self.user, line=make_line(self.user), to_type="esim"), self.staff,
        )

    def test_authoriser_creates_one_pending_command_and_moves_case(self):
        command = commands.authorise_swap(self.case, authoriser=self.staff.authoriser)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.PROVIDER_PENDING)
        self.assertEqual((command.status, command.operation), ("PENDING", "SIM_CARD_SWAP"))
        self.assertEqual(command.authorised_by, self.staff.authoriser)
        self.assertEqual(command.tenant, self.case.tenant)

    def test_verifier_cannot_authorise(self):
        with self.assertRaises(TransitionError) as ctx:
            commands.authorise_swap(self.case, authoriser=self.staff.agent)
        self.assertEqual(ctx.exception.code, "SEPARATION_OF_DUTIES")
        self.assertFalse(ProviderCommand.objects.exists())

    def test_only_reserved_cases_can_be_authorised(self):
        commands.authorise_swap(self.case, authoriser=self.staff.authoriser)
        with self.assertRaises(TransitionError) as ctx:  # second authorisation for the same case
            commands.authorise_swap(self.case, authoriser=self.staff.authoriser)
        self.assertEqual(ctx.exception.code, "TRANSITION_NOT_ALLOWED")
        self.assertEqual(ProviderCommand.objects.count(), 1)


@TEST_KEYS
class ManualExecutionTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.staff = Staff()
        self.case = to_stock_reserved(
            open_case(owner=self.user, line=make_line(self.user), to_type="esim"), self.staff,
        )
        self.command = commands.authorise_swap(self.case, authoriser=self.staff.authoriser)
        self.case.refresh_from_db()

    def _execute(self, executor, iccid=None, ticket="OPS-42"):
        return commands.record_manual_execution(
            self.command, executor=executor, target_iccid=iccid or self.case.new_sim.iccid,
            ticket_reference=ticket, correlation_id="TSL-TX-9",
        )

    def test_execution_marks_sent_and_moves_to_swap_in_progress(self):
        command = self._execute(self.staff.executor)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.SWAP_IN_PROGRESS)
        self.assertIsNotNone(self.case.supplier_submitted_at)
        self.assertEqual(self.case.supplier_correlation_id, "TSL-TX-9")
        self.assertEqual((command.status, command.executed_by), ("SENT", self.staff.executor))
        self.assertEqual(command.target_serial_masked, "…" + self.case.new_sim.iccid[-4:])
        event = SimTypeChangeEvent.objects.get(case=self.case, kind="command_executed")
        self.assertNotIn(self.case.new_sim.iccid, str(event.detail))

    def test_verifier_cannot_execute(self):
        with self.assertRaises(TransitionError) as ctx:
            self._execute(self.staff.agent)
        self.assertEqual(ctx.exception.code, "SEPARATION_OF_DUTIES")

    def test_authoriser_may_execute_by_default(self):
        self._execute(self.staff.authoriser)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.SWAP_IN_PROGRESS)

    @override_settings(SIM_CHANGE_EXECUTOR_MUST_DIFFER_FROM_AUTHORISER=True)
    def test_authoriser_cannot_execute_when_configured(self):
        with self.assertRaises(TransitionError) as ctx:
            self._execute(self.staff.authoriser)
        self.assertEqual(ctx.exception.code, "SEPARATION_OF_DUTIES")

    def test_wrong_target_sim_refused(self):
        with self.assertRaises(TransitionError) as ctx:
            self._execute(self.staff.executor, iccid=make_sim(esim=True).iccid)
        self.assertEqual(ctx.exception.code, "WRONG_TARGET_SIM")
        self.command.refresh_from_db()
        self.assertEqual(self.command.status, "PENDING")

    def test_ticket_reference_required(self):
        with self.assertRaises(TransitionError) as ctx:
            self._execute(self.staff.executor, ticket=" ")
        self.assertEqual(ctx.exception.code, "TICKET_REQUIRED")

    def test_cannot_record_execution_twice(self):
        self._execute(self.staff.executor)
        with self.assertRaises(TransitionError) as ctx:
            self._execute(self.staff.executor)
        self.assertEqual(ctx.exception.code, "COMMAND_NOT_PENDING")
