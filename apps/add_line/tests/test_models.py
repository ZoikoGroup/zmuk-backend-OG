from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.plans.models import Plan
from apps.add_line.models import AddLineOrder, Line, PortRequest, SimType, Term
from apps.sims.models import Sim


class ModelConstraintTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.plan = Plan.objects.create(name="Standard 10GB", price=Decimal("10.00"), duration_days=30)
        self.sim = Sim.objects.create(iccid="8944000000000000001")

    def test_sim_backs_only_one_live_line(self):
        Line.objects.create(owner=self.user, sim=self.sim, status=Line.Status.ACTIVE)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Line.objects.create(owner=self.user, sim=self.sim)

    def test_sim_reusable_after_line_terminated(self):
        Line.objects.create(owner=self.user, sim=self.sim, status=Line.Status.TERMINATED)
        Line.objects.create(owner=self.user, sim=self.sim)  # must not raise

    def test_one_open_port_request_per_line(self):
        line = Line.objects.create(owner=self.user, sim=self.sim)
        PortRequest.objects.create(line=line, code_type=PortRequest.CodeType.PAC)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PortRequest.objects.create(line=line, code_type=PortRequest.CodeType.PAC)

    def test_new_port_request_allowed_after_invalidated(self):
        line = Line.objects.create(owner=self.user, sim=self.sim)
        PortRequest.objects.create(
            line=line, code_type=PortRequest.CodeType.PAC, status=PortRequest.Status.INVALIDATED
        )
        PortRequest.objects.create(line=line, code_type=PortRequest.CodeType.PAC)  # must not raise

    def test_order_reference_generated(self):
        order = AddLineOrder.objects.create(
            owner=self.user, plan=self.plan, term=Term.DAYS_30, sim_type=SimType.ESIM,
            switch_choice=AddLineOrder.SwitchChoice.NONE,
            total_gross=Decimal("10.00"), vat_amount=Decimal("1.67"),
        )
        self.assertRegex(order.order_reference, r"^AL-[0-9A-F]{10}$")

    def test_counts_toward_cap(self):
        self.assertTrue(Line(status=Line.Status.SUSPENDED).counts_toward_cap)
        self.assertFalse(Line(status=Line.Status.CANCELLED).counts_toward_cap)
