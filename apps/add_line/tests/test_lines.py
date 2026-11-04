from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.add_line.models import AddLineOrder, Line, LineClaim, PortRequest
from apps.add_line.services import claims
from apps.plans.models import Plan
from apps.sims.models import Order as SimOrder
from apps.sims.models import Sim

ICCID = "8944000000000000001"


@override_settings(MAX_CONSUMER_LINES=5, ADD_LINE_CLAIM_AUTO_APPROVE=True)
class ClaimTests(TestCase):
    def setUp(self):
        cache.clear()  # claim throttle
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.plan = Plan.objects.create(name="Max 30GB", price=Decimal("15"), duration_days=30,
                                        transatelID="TSL_UK_30GB")
        self.order = SimOrder.objects.create(
            order_reference="ZK-100", email="other@example.com", billing_email="Alice@Example.com",
            payload={"items": [{"transatelID": "TSL_UK_30GB", "simType": "esim", "quantity": 1}]},
        )
        self.sim = Sim.objects.create(iccid=ICCID, msisdn="447700900001", order_reference="ZK-100",
                                      inventory=Sim.Inventory.ACTIVATED, provisioning_status="Active")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def claim(self, **data):
        return self.client.post("/api/v1/lines/claims/", data, format="json")

    def test_strong_evidence_auto_links(self):
        resp = self.claim(iccid=ICCID, order_reference="ZK-100")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["status"], "approved")
        line = Line.objects.get()
        self.assertEqual((line.owner, line.sim, line.source, line.status, line.plan),
                         (self.user, self.sim, Line.Source.LEGACY_CLAIM, Line.Status.ACTIVE, self.plan))
        self.assertEqual(resp.json()["line_id"], str(line.uuid))

    def test_email_alone_never_links(self):
        """Matching email but no ICCID (no proof of possession) → queued."""
        resp = self.claim(msisdn="07700900001", order_reference="ZK-100")
        self.assertEqual(resp.json()["status"], "submitted")
        self.assertFalse(Line.objects.exists())

    def test_different_account_email_queued(self):
        self.client.force_authenticate(User.objects.create_user("bob", "bob@example.com", "pw"))
        self.assertEqual(self.claim(iccid=ICCID, order_reference="ZK-100").json()["status"], "submitted")
        self.assertFalse(Line.objects.exists())

    def test_mismatched_number_queued(self):
        resp = self.claim(iccid=ICCID, order_reference="ZK-100", msisdn="07700900999")
        self.assertEqual(resp.json()["status"], "submitted")

    def test_already_linked_sim_queued(self):
        Line.objects.create(owner=User.objects.create_user("bob", "b@example.com", "pw"), sim=self.sim,
                            status=Line.Status.ACTIVE)
        self.assertEqual(self.claim(iccid=ICCID, order_reference="ZK-100").json()["status"], "submitted")

    def test_no_match_looks_the_same_as_queued(self):
        miss = self.claim(iccid="8944999999999999999", order_reference="ZK-NOPE").json()
        queued = self.claim(msisdn="07700900001", order_reference="ZK-100").json()
        self.assertEqual({k: miss[k] for k in ("status", "message")},
                         {k: queued[k] for k in ("status", "message")})
        self.assertNotIn("evidence", miss)

    @override_settings(ADD_LINE_CLAIM_AUTO_APPROVE=False)
    def test_auto_approve_can_be_switched_off(self):
        self.assertEqual(self.claim(iccid=ICCID, order_reference="ZK-100").json()["status"], "submitted")

    def test_needs_two_identifiers(self):
        resp = self.claim(iccid=ICCID)
        self.assertEqual((resp.status_code, resp.json()["code"]), (400, "MORE_DETAILS_NEEDED"))

    def test_open_claims_capped(self):
        for i in range(claims.MAX_OPEN_CLAIMS):
            self.claim(msisdn="07700900001", order_reference=f"ZK-{i}")
        self.assertEqual(self.claim(msisdn="07700900001", order_reference="ZK-X").status_code, 429)

    def test_support_approve_and_reject(self):
        support = User.objects.create_user("support", "s@example.com", "pw", is_staff=True)
        claim = LineClaim.objects.get(pk=self.claim(msisdn="07700900001", order_reference="ZK-100").json()["id"])
        line = claims.approve(claim, reviewer=support, note="checked ID on phone")
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.reviewed_by, claim.resulting_line), ("approved", support, line))
        with self.assertRaises(claims.ClaimError):
            claims.approve(claim, reviewer=support)

        other = LineClaim.objects.create(owner=self.user, iccid="1")
        self.assertTrue(claims.reject(other, reviewer=support, note="no proof"))
        self.assertFalse(claims.reject(other, reviewer=support))

    def test_approve_refuses_sim_on_live_line(self):
        claim = LineClaim.objects.create(owner=self.user, matched_sim=self.sim)
        Line.objects.create(owner=self.user, sim=self.sim, status=Line.Status.ACTIVE)
        with self.assertRaises(claims.ClaimError) as ctx:
            claims.approve(claim, reviewer=None)
        self.assertEqual(ctx.exception.code, "SIM_ALREADY_LINKED")


@override_settings(MAX_CONSUMER_LINES=5)
class LinesListTests(TestCase):
    URL = "/api/v1/lines/"

    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.plan = Plan.objects.create(name="Max 30GB", price=Decimal("15"), duration_days=30,
                                        data_allowance="30GB", transatelID="TSL")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_requires_login(self):
        self.assertEqual(APIClient().get(self.URL).status_code, 401)

    def test_lists_own_lines_with_transfer_and_capacity(self):
        sim = Sim.objects.create(iccid="8944000000000001234", type_of_sim="eUICC")
        line = Line.objects.create(owner=self.user, sim=sim, plan=self.plan, term="12M",
                                   status=Line.Status.ACTIVE, current_msisdn="447700900001")
        PortRequest.objects.create(line=line, code_type="PAC", status=PortRequest.Status.INVALIDATED)
        PortRequest.objects.create(line=line, code_type="PAC", status=PortRequest.Status.PENDING,
                                   encrypted_code="secret", portability_msisdn="447700900123")
        Line.objects.create(owner=self.user, status=Line.Status.CANCELLED)
        Line.objects.create(owner=User.objects.create_user("bob", "b@example.com", "pw"),
                            status=Line.Status.ACTIVE)

        body = self.client.get(self.URL).json()

        self.assertEqual(len(body["lines"]), 1)
        item = body["lines"][0]
        self.assertEqual(item["msisdn"], "447700900001")
        self.assertEqual(item["plan"], {"id": self.plan.pk, "name": "Max 30GB", "data_allowance": "30GB"})
        self.assertEqual(item["sim"], {"type": "esim", "iccid_last4": "1234"})
        self.assertEqual(item["port_request"]["status"], "PENDING")  # newest
        self.assertNotIn("secret", str(body))
        self.assertEqual(body["capacity"], {"used": 1, "limit": 5, "remaining": 4})

    def test_paid_order_without_line_shows_as_pending(self):
        AddLineOrder.objects.create(
            owner=self.user, plan=self.plan, term="30D", sim_type="esim", switch_choice="none",
            total_gross=Decimal("15"), vat_amount=Decimal("2.50"), payment_status="paid",
            fulfilment_status="failed",
        )
        AddLineOrder.objects.create(
            owner=self.user, plan=self.plan, term="30D", sim_type="esim", switch_choice="none",
            total_gross=Decimal("15"), vat_amount=Decimal("2.50"), fulfilment_status="cancelled",
        )
        body = self.client.get(self.URL).json()
        self.assertEqual(body["lines"], [])
        self.assertEqual(len(body["pending_orders"]), 1)
        self.assertIn("finishing setting up", body["pending_orders"][0]["message"])
