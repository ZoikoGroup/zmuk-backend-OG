from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.add_line.models import PortRequest
from apps.sim_change import states
from apps.sim_change.models import LineOperationLock, ProviderCommand, SimTypeChangeCase
from apps.sim_change.services import crypto
from apps.sim_change.services.transitions import TransitionError, open_case, set_fulfilment, transition

from .helpers import (
    CUST, POLL, S, SUP, SYS, TEST_KEYS, Staff, deliver_physical, make_evidence, make_grant, make_line, make_user,
    to_stock_reserved, to_swap_in_progress, to_verified,
)


@TEST_KEYS
class OpenCaseTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.line = make_line(self.user)  # physical SIM

    def test_opens_requested_case_and_takes_line_lock(self):
        case = open_case(owner=self.user, line=self.line, to_type="esim")
        self.assertEqual(case.status, S.REQUESTED)
        self.assertEqual((case.from_type, case.direction), ("psim", "psim_to_esim"))
        self.assertEqual(case.old_sim_id, self.line.sim_id)
        self.assertEqual(case.tenant, "ZMUK")
        lock = LineOperationLock.objects.get(line=self.line, released_at__isnull=True)
        self.assertEqual((lock.reference, lock.tenant), (str(case.uuid), "ZMUK"))
        self.assertEqual(case.events.get().kind, "case_opened")

    def test_track_that_does_not_apply_is_not_required(self):
        case = open_case(owner=self.user, line=self.line, to_type="esim")
        self.assertEqual((case.physical_fulfilment, case.esim_fulfilment), ("NOT_REQUIRED", ""))

    @override_settings(SIM_CHANGE_TENANT="OTHER")
    def test_tenant_comes_from_server_settings(self):
        self.assertEqual(open_case(owner=self.user, line=self.line, to_type="esim").tenant, "OTHER")

    def test_snapshot_keeps_fingerprints_not_raw_identifiers(self):
        case = open_case(owner=self.user, line=self.line, to_type="esim")
        snapshot = str(case.before_snapshot)
        self.assertNotIn(self.line.current_msisdn, snapshot)
        self.assertNotIn(self.line.sim.iccid, snapshot)
        self.assertTrue(crypto.same_identifier(case.before_snapshot["msisdn_fingerprint"], "+44 7700 900123"))
        self.assertTrue(crypto.same_identifier(case.before_snapshot["old_iccid_fingerprint"], self.line.sim.iccid))

    def test_second_case_on_same_line_refused(self):
        open_case(owner=self.user, line=self.line, to_type="esim")
        with self.assertRaises(TransitionError) as ctx:
            open_case(owner=self.user, line=self.line, to_type="esim")
        self.assertEqual(ctx.exception.code, "LINE_OPERATION_IN_PROGRESS")

    def test_port_in_progress_blocks(self):
        PortRequest.objects.create(line=self.line, code_type=PortRequest.CodeType.PAC, status=PortRequest.Status.PENDING)
        with self.assertRaises(TransitionError) as ctx:
            open_case(owner=self.user, line=self.line, to_type="esim")
        self.assertEqual(ctx.exception.code, "PORT_IN_PROGRESS")
        self.assertFalse(SimTypeChangeCase.objects.exists())

    def test_finished_port_does_not_block(self):
        PortRequest.objects.create(line=self.line, code_type=PortRequest.CodeType.PAC, status=PortRequest.Status.DONE)
        open_case(owner=self.user, line=self.line, to_type="esim")  # must not raise

    def test_someone_elses_line_is_not_found(self):
        with self.assertRaises(TransitionError) as ctx:
            open_case(owner=make_user("bob"), line=self.line, to_type="esim")
        self.assertEqual(ctx.exception.http_status, 404)

    def test_same_type_target_rejected(self):
        with self.assertRaises(TransitionError) as ctx:
            open_case(owner=self.user, line=self.line, to_type="psim")
        self.assertEqual(ctx.exception.code, "INVALID_TARGET")

    def test_missing_fingerprint_key_fails_closed(self):
        with override_settings(SIM_CHANGE_FINGERPRINT_KEY=""):
            with self.assertRaises(crypto.CryptoUnavailable):
                open_case(owner=self.user, line=self.line, to_type="esim")
        self.assertFalse(LineOperationLock.objects.exists())


@TEST_KEYS
class FlowTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.staff = Staff()

    def test_esim_to_physical_full_flow(self):
        line = make_line(self.user, esim=True)
        case = to_stock_reserved(open_case(owner=self.user, line=line, to_type="psim"), self.staff)
        case = deliver_physical(case, self.staff)
        case = to_swap_in_progress(case, self.staff)
        case = transition(case, S.SUPPLIER_BINDING_CONFIRMED, source=POLL, evidence=make_evidence(case))
        case = transition(case, S.SERVICE_READY, source=SYS)
        case = transition(case, S.CLOSED, source=SUP)

        self.assertEqual(case.status, S.CLOSED)
        self.assertEqual(case.physical_fulfilment, "RECEIPT_CONFIRMED")
        self.assertEqual((case.carrier, case.tracking_reference), ("Royal Mail", "RM123456789GB"))
        self.assertEqual(case.dispatched_by, self.staff.authoriser)
        for stamp in ("approved_at", "supplier_submitted_at", "binding_confirmed_at", "service_ready_at", "closed_at"):
            self.assertIsNotNone(getattr(case, stamp), stamp)
        self.assertEqual(case.version, case.events.exclude(kind__startswith="command_").count())
        self.assertFalse(LineOperationLock.objects.filter(line=line, released_at__isnull=True).exists())

    def test_physical_to_esim_follows_uk_sequence(self):
        case = to_stock_reserved(open_case(owner=self.user, line=make_line(self.user), to_type="esim"), self.staff)
        # Install access can't open before the supplier confirms the swap.
        with self.assertRaises(TransitionError) as ctx:
            set_fulfilment(case, "esim", "PROFILE_AVAILABLE", source=SYS)
        self.assertEqual(ctx.exception.code, "BINDING_NOT_CONFIRMED")

        case = to_swap_in_progress(case, self.staff)
        case = transition(case, S.SUPPLIER_BINDING_CONFIRMED, source=POLL, evidence=make_evidence(case))
        with self.assertRaises(TransitionError) as ctx:
            transition(case, S.SERVICE_READY, source=SYS)
        self.assertEqual(ctx.exception.code, "ESIM_SETUP_NOT_READY")

        case = set_fulfilment(case, "esim", "PROFILE_AVAILABLE", source=SYS)
        case = set_fulfilment(case, "esim", "ACCESS_READY", source=SYS)
        case = transition(case, S.SERVICE_READY, source=SYS)
        case = set_fulfilment(case, "esim", "ACCESS_ISSUED", source=SYS)
        case = set_fulfilment(case, "esim", "INSTALLATION_REPORTED", source=CUST)
        self.assertEqual((case.status, case.esim_fulfilment), (S.SERVICE_READY, "INSTALLATION_REPORTED"))

    def test_main_lifecycle_has_no_delivery_states(self):
        for name in ("SHIPPING", "DELIVERED", "RECEIPT_CONFIRMED", "PROFILE_READY", "SECURE_INSTALL_ACCESS"):
            self.assertNotIn(name, S.values)


@TEST_KEYS
class PhysicalFulfilmentTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.staff = Staff()
        self.case = open_case(owner=self.user, line=make_line(self.user, esim=True), to_type="psim")

    def test_no_dispatch_before_stock_reserved(self):
        case = to_verified(self.case, self.staff)
        with self.assertRaises(TransitionError) as ctx:
            set_fulfilment(case, "physical", "AWAITING_DISPATCH", source=SUP)
        self.assertEqual(ctx.exception.code, "NO_STOCK_RESERVED")

    def test_shipping_needs_carrier_and_tracking(self):
        case = to_stock_reserved(self.case, self.staff)
        case = set_fulfilment(case, "physical", "AWAITING_DISPATCH", source=SUP)
        with self.assertRaises(TransitionError) as ctx:
            set_fulfilment(case, "physical", "SHIPPED", source=SUP, carrier="Royal Mail")
        self.assertEqual(ctx.exception.code, "TRACKING_REQUIRED")

    def test_swap_waits_for_receipt_not_courier_delivery(self):
        case = to_stock_reserved(self.case, self.staff)
        case = set_fulfilment(case, "physical", "AWAITING_DISPATCH", source=SUP)
        case = set_fulfilment(case, "physical", "SHIPPED", source=SUP, carrier="DPD", tracking_reference="1")
        case = set_fulfilment(case, "physical", "DELIVERED", source=SUP)
        from apps.sim_change.services import commands
        with self.assertRaises(TransitionError) as ctx:
            commands.authorise_swap(case, authoriser=self.staff.authoriser)
        self.assertEqual(ctx.exception.code, "RECEIPT_NOT_CONFIRMED")
        self.assertFalse(ProviderCommand.objects.exists())  # rolled back with the refused transition

    def test_customer_can_only_confirm_receipt(self):
        case = to_stock_reserved(self.case, self.staff)
        with self.assertRaises(TransitionError) as ctx:
            set_fulfilment(case, "physical", "AWAITING_DISPATCH", source=CUST)
        self.assertEqual(ctx.exception.code, "NOT_PERMITTED")

    def test_lost_parcel_goes_to_exception_and_can_be_redispatched(self):
        case = to_stock_reserved(self.case, self.staff)
        case = set_fulfilment(case, "physical", "AWAITING_DISPATCH", source=SUP)
        case = set_fulfilment(case, "physical", "SHIPPED", source=SUP, carrier="DPD", tracking_reference="1")
        case = set_fulfilment(case, "physical", "EXCEPTION", source=SUP, reason="lost in post")
        case = set_fulfilment(case, "physical", "AWAITING_DISPATCH", source=SUP)
        self.assertEqual(case.physical_fulfilment, "AWAITING_DISPATCH")

    def test_wrong_track_for_target_refused(self):
        with self.assertRaises(TransitionError):
            set_fulfilment(self.case, "esim", "PROFILE_AVAILABLE", source=SYS)

    def test_fraud_hold_freezes_fulfilment(self):
        case = to_stock_reserved(self.case, self.staff)
        case = transition(case, S.HOLD_FRAUD, source=CUST)
        with self.assertRaises(TransitionError) as ctx:
            set_fulfilment(case, "physical", "AWAITING_DISPATCH", source=SUP)
        self.assertEqual(ctx.exception.code, "CASE_ON_HOLD")


@TEST_KEYS
class GuardTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.staff = Staff()
        self.line = make_line(self.user)
        self.case = open_case(owner=self.user, line=self.line, to_type="esim")

    def _verify_required(self):
        case = transition(self.case, S.ELIGIBILITY_PENDING, source=SYS)
        return transition(case, S.VERIFY_REQUIRED, source=SYS)

    def _submitted(self):
        return to_swap_in_progress(to_stock_reserved(self.case, self.staff), self.staff)

    def test_unlisted_move_rejected(self):
        with self.assertRaises(TransitionError) as ctx:
            transition(self.case, S.SERVICE_READY, source=SYS)
        self.assertEqual(ctx.exception.code, "TRANSITION_NOT_ALLOWED")

    def test_customer_cannot_drive_internal_steps(self):
        with self.assertRaises(TransitionError) as ctx:
            transition(self.case, S.ELIGIBILITY_PENDING, source=CUST)
        self.assertEqual(ctx.exception.code, "NOT_PERMITTED")

    def test_stale_version_rejected(self):
        with self.assertRaises(TransitionError) as ctx:
            transition(self.case, S.ELIGIBILITY_PENDING, source=SYS, expected_version=self.case.version + 1)
        self.assertEqual(ctx.exception.code, "CASE_CHANGED")

    def test_stock_reserved_needs_a_sim(self):
        case = to_verified(self.case, self.staff)
        for status in (S.QUOTED, S.AWAITING_CONFIRMATION):
            case = transition(case, status, source=SYS)
        case = transition(case, S.CASE_ACCEPTED, source=CUST)
        with self.assertRaises(TransitionError) as ctx:
            transition(case, S.STOCK_RESERVED, source=SUP)
        self.assertEqual(ctx.exception.code, "NO_STOCK_RESERVED")

    def test_provider_pending_needs_authorised_command(self):
        case = to_stock_reserved(self.case, self.staff)
        with self.assertRaises(TransitionError) as ctx:
            transition(case, S.PROVIDER_PENDING, source=SUP)
        self.assertEqual(ctx.exception.code, "SWAP_NOT_AUTHORISED")

    # ── Step-up ──

    def test_verified_needs_a_grant(self):
        with self.assertRaises(TransitionError) as ctx:
            transition(self._verify_required(), S.VERIFIED, source=SUP)
        self.assertEqual(ctx.exception.code, "STEP_UP_REQUIRED")

    def test_grant_is_consumed(self):
        case = self._verify_required()
        grant = make_grant(case, self.staff.agent)
        transition(case, S.VERIFIED, source=SUP, step_up=grant)
        grant.refresh_from_db()
        self.assertIsNotNone(grant.consumed_at)

    def test_grant_problems_refused(self):
        case = self._verify_required()
        other_case = open_case(owner=self.user, line=make_line(self.user), to_type="esim")
        cases = {
            "STEP_UP_USED": dict(consumed_at=timezone.now()),
            "STEP_UP_EXPIRED": dict(expires_at=timezone.now() - timedelta(seconds=1)),
            "STEP_UP_NOT_PASSED": dict(assurance_result="referred"),
            "STEP_UP_MISMATCH": dict(purpose="esim_install_access"),
        }
        for code, overrides in cases.items():
            with self.subTest(code):
                with self.assertRaises(TransitionError) as ctx:
                    transition(case, S.VERIFIED, source=SUP, step_up=make_grant(case, self.staff.agent, **overrides))
                self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(TransitionError) as ctx:  # grant issued for a different case
            transition(case, S.VERIFIED, source=SUP, step_up=make_grant(other_case, self.staff.agent))
        self.assertEqual(ctx.exception.code, "STEP_UP_MISMATCH")

    def test_high_risk_grant_needs_a_different_approver(self):
        case = self._verify_required()
        for approver in (None, self.staff.agent):
            grant = make_grant(case, self.staff.agent, risk_tier="high", approved_by=approver)
            with self.assertRaises(TransitionError) as ctx:
                transition(case, S.VERIFIED, source=SUP, step_up=grant)
            self.assertEqual(ctx.exception.code, "SECOND_APPROVAL_REQUIRED")
        grant = make_grant(case, self.staff.agent, risk_tier="high", approved_by=self.staff.authoriser)
        self.assertEqual(transition(case, S.VERIFIED, source=SUP, step_up=grant).status, S.VERIFIED)

    # ── Supplier evidence ──

    def test_binding_needs_complete_fresh_evidence_for_this_case(self):
        case = self._submitted()
        other = open_case(owner=self.user, line=make_line(self.user), to_type="esim")
        bad = {
            "EVIDENCE_REQUIRED": None,
            "BINDING_NOT_PROVEN": make_evidence(case, old_serial_detached=False),
            "EVIDENCE_STALE": make_evidence(case, fetched_at=timezone.now() - timedelta(hours=1)),
        }
        for code, evidence in bad.items():
            with self.subTest(code), self.assertRaises(TransitionError) as ctx:
                transition(case, S.SUPPLIER_BINDING_CONFIRMED, source=POLL, evidence=evidence)
            self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(TransitionError) as ctx:
            transition(case, S.SUPPLIER_BINDING_CONFIRMED, source=POLL, evidence=make_evidence(other))
        self.assertEqual(ctx.exception.code, "EVIDENCE_REQUIRED")

    def test_changed_plan_or_term_blocks_confirmation(self):
        case = self._submitted()
        with self.assertRaises(TransitionError) as ctx:
            transition(case, S.SUPPLIER_BINDING_CONFIRMED, source=POLL,
                       evidence=make_evidence(case, commercial_terms_unchanged=False))
        self.assertEqual(ctx.exception.code, "BINDING_NOT_PROVEN")

    def test_staff_cannot_manufacture_binding_or_service_ready(self):
        case = self._submitted()
        with self.assertRaises(TransitionError):
            transition(case, S.SUPPLIER_BINDING_CONFIRMED, source=SUP, actor=self.staff.authoriser)
        with self.assertRaises(TransitionError):
            transition(case, S.SERVICE_READY, source=SUP, actor=self.staff.authoriser)

    # ── Cancel / fail ──

    def test_cancel_before_execution_releases_lock_and_unexecuted_command(self):
        case = to_stock_reserved(self.case, self.staff)
        from apps.sim_change.services import commands
        commands.authorise_swap(case, authoriser=self.staff.authoriser)
        case = transition(case, S.CANCELLED, source=CUST)
        self.assertEqual(case.commands.get().status, ProviderCommand.Status.CANCELLED)
        self.assertFalse(LineOperationLock.objects.filter(line=self.line, released_at__isnull=True).exists())

    def test_cannot_cancel_after_execution(self):
        case = transition(self._submitted(), S.SUPPORT_REQUIRED, source=SUP)
        with self.assertRaises(TransitionError) as ctx:
            transition(case, S.CANCELLED, source=CUST)
        self.assertEqual(ctx.exception.code, "CANCELLATION_UNAVAILABLE")

    def test_swap_in_progress_cannot_fail_directly(self):
        self.assertNotIn(S.FAILED, states.allowed_targets(self._submitted()))

    def test_fail_after_execution_needs_proof_swap_did_not_happen(self):
        case = transition(self._submitted(), S.RECONCILIATION_REQUIRED, source=SYS)
        with self.assertRaises(TransitionError) as ctx:
            transition(case, S.FAILED, source=SUP)
        self.assertEqual(ctx.exception.code, "EVIDENCE_REQUIRED")
        with self.assertRaises(TransitionError) as ctx:
            transition(case, S.FAILED, source=SUP,
                       evidence=make_evidence(case, confirmed=False, current_serial_is_target=True))
        self.assertEqual(ctx.exception.code, "SWAP_OUTCOME_UNCLEAR")
        case = transition(case, S.FAILED, source=SUP, evidence=make_evidence(case, confirmed=False))
        self.assertEqual(case.status, S.FAILED)

    # ── Holds ──

    def test_hold_resumes_to_where_it_came_from(self):
        case = transition(self.case, S.ELIGIBILITY_PENDING, source=SYS)
        case = transition(case, S.HOLD_FRAUD, source=SUP)
        case = transition(case, S.SUPPORT_REQUIRED, source=SUP)  # hold → hold keeps the origin
        self.assertEqual(case.held_from_status, S.ELIGIBILITY_PENDING)
        case = transition(case, S.ELIGIBILITY_PENDING, source=SUP)
        self.assertEqual((case.status, case.held_from_status), (S.ELIGIBILITY_PENDING, ""))

    def test_customer_can_report_fraud(self):
        self.assertEqual(transition(self.case, S.HOLD_FRAUD, source=CUST).action_owner, "security")

    def test_reconciliation_can_confirm_binding_with_evidence(self):
        case = transition(self._submitted(), S.RECONCILIATION_REQUIRED, source=SYS)
        case = transition(case, S.SUPPLIER_BINDING_CONFIRMED, source=POLL, evidence=make_evidence(case))
        self.assertEqual(case.status, S.SUPPLIER_BINDING_CONFIRMED)

    def test_no_hold_customer_after_supplier_step(self):
        self.assertNotIn(S.HOLD_CUSTOMER, states.allowed_targets(self._submitted()))

    def test_terminal_case_has_no_moves(self):
        case = transition(self.case, S.CANCELLED, source=CUST)
        self.assertEqual(states.allowed_targets(case), set())
        self.assertEqual(states.allowed_fulfilment_targets(case, "esim"), set())
