"""Swap SIM stock (CTO Addendum v2.1 B4; acceptance tests 2 and 8)."""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.add_line.models import Line
from apps.sim_change.models import SwapSimStock
from apps.sim_change.services import stock
from apps.sim_change.services.stock_import import StockImportError, import_swap_stock
from apps.sim_change.services.transitions import open_case, set_fulfilment, transition
from apps.sims import reservations
from apps.sims.models import Sim

from .helpers import CUST, S, SUP, SYS, TEST_KEYS, Staff, make_line, make_user, to_verified

_n = {"i": 0}


def swap_stock(fmt="esim", **overrides):
    _n["i"] += 1
    sim = Sim.objects.create(
        iccid=f"89449900000000{_n['i']:05d}", type_of_sim="eUICC" if fmt == "esim" else "UICC",
        inventory_purpose=Sim.Purpose.SWAP, provisioning_status="Available",
    )
    values = dict(sim=sim, sim_format=fmt, source="test", supplier_status="AVAILABLE")
    values.update(overrides)
    return SwapSimStock.objects.create(**values)


def accepted_case(user, staff, to_type="esim"):
    case = open_case(owner=user, line=make_line(user, esim=to_type == "psim"), to_type=to_type)
    case = to_verified(case, staff)
    case = transition(case, S.QUOTED, source=SYS)
    case = transition(case, S.AWAITING_CONFIRMATION, source=SYS)
    return transition(case, S.CASE_ACCEPTED, source=CUST)


@TEST_KEYS
class ReserveTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.staff = Staff()
        self.case = accepted_case(self.user, self.staff)

    def reserve(self, **kw):
        return stock.reserve_for_case(self.case, actor=self.staff.authoriser, supplier_check_reference="AUR-1", **kw)

    def test_reserves_oldest_eligible_and_moves_case(self):
        first, _second = swap_stock(), swap_stock()
        reserved = self.reserve()
        self.case.refresh_from_db()
        self.assertEqual(reserved.pk, first.pk)
        self.assertEqual(self.case.status, S.STOCK_RESERVED)
        self.assertEqual(self.case.new_sim_id, first.sim_id)
        self.assertEqual((reserved.status, reserved.reserved_case_id), ("RESERVED", self.case.pk))
        self.assertIsNotNone(reserved.reservation_expires_at)
        first.sim.refresh_from_db()
        self.assertEqual(first.sim.inventory, Sim.Inventory.RESERVED)

    def test_ineligible_stock_rejected(self):
        other_line_owner = make_user("bob")
        cases = {
            "NOT_A_SWAP_SIM": lambda s: Sim.objects.filter(pk=s.sim_id).update(inventory_purpose="sales"),
            "SWAP_SIM_HAS_NUMBER": lambda s: Sim.objects.filter(pk=s.sim_id).update(msisdn="447700900999"),
            "SWAP_SIM_WRONG_TENANT": lambda s: SwapSimStock.objects.filter(pk=s.pk).update(tenant="OTHER"),
            "SWAP_SIM_NOT_AVAILABLE": lambda s: SwapSimStock.objects.filter(pk=s.pk).update(status="QUARANTINED"),
            "SWAP_SIM_ASSIGNED": lambda s: Line.objects.create(
                owner=other_line_owner, sim=s.sim, status=Line.Status.ACTIVE),
        }
        for code, spoil in cases.items():
            with self.subTest(code):
                candidate = swap_stock()
                spoil(candidate)
                with self.assertRaises(stock.StockError) as ctx:
                    self.reserve(stock=candidate)
                self.assertEqual(ctx.exception.code, code)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, S.CASE_ACCEPTED)

    def test_wrong_format_rejected(self):
        with self.assertRaises(stock.StockError) as ctx:
            self.reserve(stock=swap_stock(fmt="psim"))
        self.assertEqual(ctx.exception.code, "SWAP_SIM_WRONG_FORMAT")

    def test_no_eligible_stock_is_reported(self):
        swap_stock(fmt="psim")  # wrong format only
        with self.assertRaises(stock.StockError) as ctx:
            self.reserve()
        self.assertEqual(ctx.exception.code, "NO_SWAP_STOCK")

    def test_supplier_check_reference_required(self):
        swap_stock()
        with self.assertRaises(stock.StockError) as ctx:
            stock.reserve_for_case(self.case, actor=self.staff.authoriser, supplier_check_reference=" ")
        self.assertEqual(ctx.exception.code, "SUPPLIER_CHECK_REQUIRED")

    def test_reserved_sim_cannot_be_reserved_again(self):
        candidate = swap_stock()
        self.reserve(stock=candidate)
        second_case = accepted_case(make_user("dave"), self._other_staff())
        with self.assertRaises(stock.StockError) as ctx:
            stock.reserve_for_case(second_case, actor=self.staff.authoriser, supplier_check_reference="AUR-2",
                                   stock=candidate)
        self.assertEqual(ctx.exception.code, "SWAP_SIM_NOT_AVAILABLE")

    def _other_staff(self):
        staff = Staff.__new__(Staff)
        staff.agent = make_user("agent2", staff=True)
        staff.authoriser, staff.executor = self.staff.authoriser, self.staff.executor
        return staff

    def test_cancel_before_dispatch_returns_sim_to_stock(self):
        reserved = self.reserve(stock=swap_stock())
        transition(self.case, S.CANCELLED, source=CUST)
        reserved.refresh_from_db()
        self.assertEqual((reserved.status, reserved.reserved_case_id), ("AVAILABLE", None))
        reserved.sim.refresh_from_db()
        self.assertEqual((reserved.sim.inventory, reserved.sim.order_reference), (Sim.Inventory.IN_STOCK, ""))

    def test_cancel_after_dispatch_quarantines_sim(self):
        case = accepted_case(make_user("erin"), self._other_staff(), to_type="psim")
        reserved = stock.reserve_for_case(case, actor=self.staff.authoriser, supplier_check_reference="AUR-3",
                                          stock=swap_stock(fmt="psim"))
        case.refresh_from_db()
        case = set_fulfilment(case, "physical", "AWAITING_DISPATCH", source=SUP)
        case = set_fulfilment(case, "physical", "SHIPPED", source=SUP, carrier="DPD", tracking_reference="1")
        transition(case, S.CANCELLED, source=CUST)
        reserved.refresh_from_db()
        self.assertEqual(reserved.status, "QUARANTINED")

    def test_expired_reservation_flags_case_to_support_and_keeps_sim(self):
        reserved = self.reserve(stock=swap_stock())
        SwapSimStock.objects.filter(pk=reserved.pk).update(reservation_expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(stock.flag_expired_reservations(), 1)
        self.case.refresh_from_db()
        reserved.refresh_from_db()
        self.assertEqual((self.case.status, self.case.held_from_status), (S.SUPPORT_REQUIRED, S.STOCK_RESERVED))
        self.assertEqual(reserved.status, "RESERVED")
        self.assertEqual(stock.flag_expired_reservations(), 0)  # not flagged twice


class ShopNeverSellsSwapSimsTests(TestCase):
    def test_swap_sim_excluded_from_sales_queries_even_with_a_number(self):
        Sim.objects.create(iccid="8944880000000000001", type_of_sim="eUICC", msisdn="447700900111",
                           provisioning_status="Available", inventory_purpose=Sim.Purpose.SWAP)
        sale = Sim.objects.create(iccid="8944880000000000002", type_of_sim="eUICC", msisdn="447700900222",
                                  provisioning_status="Available")
        self.assertEqual(list(reservations._available_queryset("esim")), [sale])
        self.assertEqual(reservations.latest_available_sim(), sale)
        self.assertEqual(reservations.count_available("esim"), 1)


class ImportTests(TestCase):
    def run_import(self, body, **kw):
        header = "iccid,sim_format,supplier_status,supplier_pool_id,msisdn\n"
        return import_swap_stock((header + body).encode(), source="Ops list 1", **kw)

    def test_valid_file_creates_swap_sims_and_stock(self):
        result = self.run_import("89440000000000000011,esim,AVAILABLE,UK-1,\n8944 0000 0000 0000 0012,psim,AVAILABLE,,\n",
                                 pool="UK-DEFAULT")
        self.assertEqual((result.created, result.updated, result.problems), (2, 0, []))
        sim = Sim.objects.get(iccid="89440000000000000012")
        self.assertEqual((sim.inventory_purpose, sim.sim_type_key, sim.msisdn), ("swap", "psim", ""))
        self.assertEqual(sim.swap_stock.supplier_pool_id, "UK-DEFAULT")
        self.assertEqual(sim.swap_stock.status, "AVAILABLE")

    def test_any_problem_saves_nothing(self):
        Sim.objects.create(iccid="89440000000000000099", type_of_sim="eUICC", msisdn="447700900123")
        result = self.run_import(
            "89440000000000000021,esim,,,\n"      # fine
            "89440000000000000099,esim,,,\n"      # existing Sales SIM
            "89440000000000000022,esim,,,4477001\n"  # has a number
            "12345,esim,,,\n"                      # bad ICCID
            "89440000000000000023,nano,,,\n"       # bad format
            "89440000000000000021,esim,,,\n"       # duplicate
        )
        rules = {(p.row, p.field) for p in result.problems}
        self.assertEqual(rules, {(3, "iccid"), (4, "msisdn"), (5, "iccid"), (6, "sim_format"), (7, "iccid")})
        self.assertFalse(SwapSimStock.objects.exists())
        self.assertFalse(Sim.objects.filter(iccid="89440000000000000021").exists())

    def test_dry_run_saves_nothing(self):
        result = self.run_import("89440000000000000031,esim,,,\n", dry_run=True)
        self.assertEqual((result.created, result.problems), (1, []))
        self.assertFalse(Sim.objects.filter(iccid="89440000000000000031").exists())

    def test_reimport_updates_supplier_fields_but_not_reserved_stock(self):
        self.run_import("89440000000000000041,esim,AVAILABLE,UK-1,\n")
        result = self.run_import("89440000000000000041,esim,CHECKED,UK-2,\n")
        self.assertEqual(result.updated, 1)
        row = SwapSimStock.objects.get(sim__iccid="89440000000000000041")
        self.assertEqual((row.supplier_status, row.supplier_pool_id), ("CHECKED", "UK-2"))
        SwapSimStock.objects.filter(pk=row.pk).update(status="ASSIGNED")
        result = self.run_import("89440000000000000041,esim,AVAILABLE,UK-1,\n")
        self.assertEqual(result.problems[0].field, "iccid")

    def test_missing_columns_and_source_rejected(self):
        with self.assertRaises(StockImportError):
            import_swap_stock(b"iccid\n8944\n", source="x")
        with self.assertRaises(StockImportError):
            import_swap_stock(b"iccid,sim_format\n89440000000000000051,esim\n", source=" ")


@TEST_KEYS
@override_settings(SIM_CHANGE_TENANT="ZMUK")
class ImportTenantTests(TestCase):
    def test_imported_stock_uses_server_tenant(self):
        import_swap_stock(b"iccid,sim_format\n89440000000000000061,esim\n", source="x")
        self.assertEqual(SwapSimStock.objects.get().tenant, "ZMUK")
