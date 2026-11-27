"""Staff roles, work-queue actions and separation of duties
(CTO Addendum v2.1 A3; acceptance tests 3, 4, 5)."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.add_line.models import Line
from apps.plans.models import Plan
from apps.sim_change.admin_actions import ACTIONS
from apps.sim_change.models import SimChangePlanEligibility, SimTypeChangeCase, StatusSource, SwapSimStock
from apps.sim_change.services import address, operations, requests as req
from apps.sim_change.services.roles import ROLES
from apps.sim_change.services.transitions import TransitionError, open_case, set_fulfilment, transition
from apps.sims.models import Sim

from .helpers import S, SYS, TEST_KEYS, make_sim

AGENT, AUTHORISER, EXECUTOR, FRAUD, QA = (
    "SIM change: Security Support Agent", "SIM change: Network Ops Authoriser", "SIM change: Network Ops Executor",
    "SIM change: Security / Fraud", "SIM change: QA / Audit",
)
_n = {"i": 0}


def staff_user(name, *groups):
    user = User.objects.create_user(name, f"{name}@zoiko.test", "pw-123456", is_staff=True)
    for g in groups:
        user.groups.add(Group.objects.get(name=g))
    return User.objects.get(pk=user.pk)  # fresh permission cache


def swap_stock(fmt):
    _n["i"] += 1
    sim = Sim.objects.create(iccid=f"89447700000000{_n['i']:05d}", type_of_sim="eUICC" if fmt == "esim" else "UICC",
                             inventory_purpose=Sim.Purpose.SWAP)
    return SwapSimStock.objects.create(sim=sim, sim_format=fmt, source="test")


@TEST_KEYS
class OpsTestBase(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user("cust", "cust@example.com", "pw-123456")
        self.plan = Plan.objects.create(name="Standard", price=Decimal("10.00"), duration_days=30)
        SimChangePlanEligibility.objects.create(plan=self.plan, version="1", enabled=True, psim_to_esim=True,
                                                esim_to_psim=True, effective_from=timezone.now() - timedelta(days=1))
        self.agent = staff_user("agent", AGENT)
        self.fraud = staff_user("fraud", FRAUD)
        self.authoriser = staff_user("authoriser", AUTHORISER)
        self.executor = staff_user("executor", EXECUTOR)
        self.qa = staff_user("qa", QA)

    def new_case(self, to_type="esim", **line_extra):
        line = Line.objects.create(owner=self.customer, sim=make_sim(esim=to_type == "psim"), plan=self.plan,
                                   status=Line.Status.ACTIVE, current_msisdn="447700900123", **line_extra)
        case = open_case(owner=self.customer, line=line, to_type=to_type)
        case = transition(case, S.ELIGIBILITY_PENDING, source=SYS)
        if to_type == "psim":
            case = address.set_delivery_address(case, {"line1": "1 High St", "town": "Leeds", "postcode": "LS1 1BA"},
                                                source="on_file")
        return transition(case, S.VERIFY_REQUIRED, source=SYS)

    def verify(self, case, agent=None, assurance="passed"):
        return operations.record_verification(case, agent=agent or self.agent, method="supervised_support",
                                              assurance=assurance, policy_version="SEC-VER-1")

    def to_accepted(self, case):
        self.verify(case)
        case.refresh_from_db()
        return req.confirm_request(self.customer, case, {"quote_hash": case.quote_hash})


class RoleTests(OpsTestBase):
    def test_groups_exist_with_their_permissions(self):
        for name, codenames in ROLES.items():
            group = Group.objects.get(name=name)
            self.assertEqual(set(group.permissions.values_list("codename", flat=True)), set(codenames))

    def test_qa_can_only_view(self):
        self.assertTrue(self.qa.has_perm("sim_change.view_simtypechangecase"))
        for key in operations.PERM:
            self.assertFalse(self.qa.has_perm(operations.PERM[key]), key)

    def test_no_work_queue_action_can_confirm_or_complete(self):
        keys = {a.key for a in ACTIONS}
        self.assertFalse(keys & {"confirm_binding", "service_ready", "complete"})
        # "close" exists, but only once a supplier read has made the service ready.
        close = next(a for a in ACTIONS if a.key == "close")
        for status in SimTypeChangeCase.Status.values:
            probe = SimTypeChangeCase(status=status)
            self.assertEqual(close.available(probe), status == S.SERVICE_READY, status)

    def test_non_staff_with_permission_is_still_refused(self):
        self.agent.is_staff = False
        self.agent.save()
        with self.assertRaises(TransitionError) as ctx:
            self.verify(self.new_case(), agent=User.objects.get(pk=self.agent.pk))
        self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")


class VerificationTests(OpsTestBase):
    def test_wrong_role_refused(self):
        with self.assertRaises(TransitionError) as ctx:
            self.verify(self.new_case(), agent=self.authoriser)
        self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")

    def test_standard_risk_pass_moves_to_customer_confirmation(self):
        case = self.new_case()
        grant = self.verify(case)
        case.refresh_from_db()
        self.assertEqual(case.status, S.AWAITING_CONFIRMATION)
        self.assertEqual((grant.verified_by, grant.policy_version), (self.agent, "SEC-VER-1"))
        grant.refresh_from_db()
        self.assertIsNotNone(grant.consumed_at)

    @override_settings(SIM_CHANGE_LEGACY_CLAIMS_ENABLED=True)
    def test_high_risk_needs_a_different_security_approver(self):
        case = self.new_case(source=Line.Source.LEGACY_CLAIM)
        case.risk_flags = ["LEGACY_CLAIMED_LINE"]
        case.save(update_fields=["risk_flags"])
        grant = self.verify(case)
        case.refresh_from_db()
        self.assertEqual(case.status, S.VERIFY_REQUIRED)
        self.assertEqual(list(operations.pending_approvals()), [grant])

        both = staff_user("both", AGENT, FRAUD)
        with self.assertRaises(TransitionError):
            operations.approve_verification(grant, approver=self.agent)  # wrong role
        grant2_case = self.new_case(source=Line.Source.LEGACY_CLAIM)
        grant2_case.risk_flags = ["LEGACY_CLAIMED_LINE"]
        grant2_case.save(update_fields=["risk_flags"])
        own = self.verify(grant2_case, agent=both)
        with self.assertRaises(TransitionError) as ctx:
            operations.approve_verification(own, approver=both)
        self.assertEqual(ctx.exception.code, "SEPARATION_OF_DUTIES")

        case = operations.approve_verification(grant, approver=self.fraud)
        self.assertEqual(case.status, S.AWAITING_CONFIRMATION)

    def test_referred_goes_to_fraud_hold_and_failed_ends_request(self):
        case = self.new_case()
        self.verify(case, assurance="referred")
        case.refresh_from_db()
        self.assertEqual(case.status, S.HOLD_FRAUD)
        case2 = self.new_case()
        self.verify(case2, assurance="failed")
        case2.refresh_from_db()
        self.assertEqual(case2.status, S.FAILED)

    def test_policy_version_required(self):
        with self.assertRaises(TransitionError) as ctx:
            operations.record_verification(self.new_case(), agent=self.agent, method="supervised_support",
                                           assurance="passed", policy_version=" ")
        self.assertEqual(ctx.exception.code, "POLICY_VERSION_REQUIRED")


class SwapFlowTests(OpsTestBase):
    def test_esim_assisted_flow_stops_at_swap_in_progress(self):
        case = self.to_accepted(self.new_case())
        stock = swap_stock("esim")
        operations.reserve_stock(case, actor=self.authoriser, supplier_check_reference="AUR-1")
        case.refresh_from_db()
        command = operations.authorise(case, actor=self.authoriser, reason="checks done")
        operations.record_execution(command, actor=self.executor, target_iccid=stock.sim.iccid,
                                    ticket_reference="OPS-9", correlation_id="TX-9")
        case.refresh_from_db()
        self.assertEqual(case.status, S.SWAP_IN_PROGRESS)
        # Nobody in the work queue can confirm it; Network Ops can only ask for a fresh supplier read.
        from apps.sim_change.admin_actions import actions_for
        for user in (self.agent, self.fraud, self.authoriser, self.executor, self.qa):
            keys = [a.key for a in actions_for(user, case)]
            self.assertEqual(keys, ["recheck"] if user == self.executor else [], user)

    def test_verifier_cannot_authorise_even_with_the_role(self):
        both = staff_user("both2", AGENT, AUTHORISER)
        case = self.new_case()
        self.verify(case, agent=both)
        case.refresh_from_db()
        case = req.confirm_request(self.customer, case, {"quote_hash": case.quote_hash})
        swap_stock("esim")
        operations.reserve_stock(case, actor=both, supplier_check_reference="AUR-2")
        case.refresh_from_db()
        with self.assertRaises(TransitionError) as ctx:
            operations.authorise(case, actor=both)
        self.assertEqual(ctx.exception.code, "SEPARATION_OF_DUTIES")

    def test_wrong_roles_refused(self):
        case = self.to_accepted(self.new_case())
        with self.assertRaises(TransitionError):
            operations.reserve_stock(case, actor=self.agent, supplier_check_reference="AUR-3")
        with self.assertRaises(TransitionError):
            operations.authorise(case, actor=self.executor)


class FulfilmentTests(OpsTestBase):
    def setUp(self):
        super().setUp()
        self.case = self.to_accepted(self.new_case(to_type="psim"))
        swap_stock("psim")
        operations.reserve_stock(self.case, actor=self.authoriser, supplier_check_reference="AUR-4")
        self.case.refresh_from_db()

    def test_dispatch_blocked_until_address_review_cleared(self):
        case = self.case
        case.risk_flags = case.risk_flags + [address.REVIEW_FLAG]
        case.save(update_fields=["risk_flags"])
        with self.assertRaises(TransitionError) as ctx:
            operations.record_dispatch(case, actor=self.executor, carrier="DPD", tracking_reference="T")
        self.assertEqual(ctx.exception.code, "ADDRESS_REVIEW_PENDING")
        with self.assertRaises(TransitionError):
            operations.clear_address_review(case, reviewer=self.authoriser, note="x")  # wrong role
        case = operations.clear_address_review(case, reviewer=self.fraud, note="Called customer on file number")
        case = operations.record_dispatch(case, actor=self.executor, carrier="DPD", tracking_reference="T")
        self.assertEqual(case.physical_fulfilment, "SHIPPED")

    def test_delivered_then_receipt_then_authorise(self):
        case = operations.record_dispatch(self.case, actor=self.executor, carrier="DPD", tracking_reference="T")
        case = operations.record_delivered(case, actor=self.executor)
        with self.assertRaises(TransitionError):
            operations.authorise(case, actor=self.authoriser)  # courier delivered isn't receipt
        case = set_fulfilment(case, "physical", "RECEIPT_CONFIRMED", source=StatusSource.CUSTOMER)
        command = operations.authorise(case, actor=self.authoriser)
        self.assertEqual(command.status, "PENDING")


class HoldTests(OpsTestBase):
    def test_fraud_hold_needs_security_fraud_to_release(self):
        case = transition(self.new_case(), S.HOLD_FRAUD, source=StatusSource.CUSTOMER)
        with self.assertRaises(TransitionError):
            operations.release_hold(case, actor=self.agent, note="looks fine")
        case = operations.release_hold(case, actor=self.fraud, note="Customer confirmed by call-back")
        self.assertEqual(case.status, S.VERIFY_REQUIRED)

    def test_reconciliation_cannot_be_released_by_staff(self):
        case = transition(self.new_case(), S.RECONCILIATION_REQUIRED, source=SYS)
        with self.assertRaises(TransitionError) as ctx:
            operations.release_hold(case, actor=self.fraud, note="x")
        self.assertEqual(ctx.exception.code, "RECONCILE_WITH_SUPPLIER")

    def test_staff_cancel_needs_reason(self):
        with self.assertRaises(TransitionError) as ctx:
            operations.staff_cancel(self.new_case(), actor=self.agent, reason=" ")
        self.assertEqual(ctx.exception.code, "REASON_REQUIRED")
        case = operations.staff_cancel(self.new_case(), actor=self.agent, reason="customer asked by phone")
        self.assertEqual(case.status, SimTypeChangeCase.Status.CANCELLED)
