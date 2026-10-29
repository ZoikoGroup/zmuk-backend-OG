from datetime import datetime, timezone

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.sims.models import Order, Sim

MY_ORDERS_URL = "/api/v1/sims/my-orders/"


def _dt(day, hour=12):
    return datetime(2026, 4, day, hour, tzinfo=timezone.utc)


class MyOrdersViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="lamarr", email="Lamarr@Example.com", password="x-Strong-pass-1"
        )
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.user).key}")

        self.esim = Sim.objects.create(
            iccid="89443042333290962940", msisdn="+447940832641",
            type_of_sim="eUICC Consumer profiles", provisioning_status="Active",
            imsi="234336570212778", rate_plan="MVNA Wholesale PAYM 7",
            customer_account="MVNA_UK_EEL_ZOIKOTELECOM",
            inventory=Sim.Inventory.ACTIVATED, order_reference="ZK-1",
            activated_at=_dt(3), last_action_date=_dt(20),
            esim_qr_value="LPA:1$smdp.example$ABC", esim_smdp_address="smdp.example",
            esim_activation_code="LPA:1$smdp.example$ABC", esim_qr_status="RELEASED",
            esim_qr_fetched_at=_dt(4), esim_qr_emailed=True,
        )
        self.psim = Sim.objects.create(
            iccid="8944122666635921879", msisdn="+447421115993",
            type_of_sim="UICC ISIM Consumer", provisioning_status="Active",
            inventory=Sim.Inventory.ACTIVATED, order_reference="ZK-1",
        )
        self.order = Order.objects.create(
            order_reference="ZK-1", email="lamarr@example.com",
            payload={"items": [{"name": "30-day eSIM", "simType": "esim", "quantity": 1,
                                "transatelID": "PKG1", "duration": 30, "dataAllowance": "20GB"}]},
            assigned=[
                {"iccid": self.esim.iccid, "msisdn": self.esim.msisdn, "sim_type": "esim",
                 "transatelID": "PKG1", "transaction_id": "tx-1", "activation": {"raw": "secret"}},
                {"iccid": self.psim.iccid, "msisdn": self.psim.msisdn, "sim_type": "psim",
                 "transatelID": "PKG2", "transaction_id": "tx-2"},
            ],
            errors=["internal transatel detail"],
            status=Order.STATUS_COMPLETED,
        )

    def test_requires_authentication(self):
        response = APIClient().get(MY_ORDERS_URL)
        self.assertEqual(response.status_code, 401)

    def test_returns_only_own_orders_case_insensitive(self):
        Order.objects.create(order_reference="ZK-OTHER", email="someone@else.com")
        Order.objects.create(order_reference="ZK-BILL", email="buyer@else.com",
                             billing_email="LAMARR@example.com")

        response = self.client.get(MY_ORDERS_URL)

        self.assertEqual(response.status_code, 200)
        refs = {o["order_reference"] for o in response.data["orders"]}
        self.assertEqual(refs, {"ZK-1", "ZK-BILL"})
        self.assertEqual(response.data["count"], 2)

    def test_query_params_cannot_select_another_users_orders(self):
        Order.objects.create(order_reference="ZK-OTHER", email="someone@else.com")
        response = self.client.get(MY_ORDERS_URL, {"email": "someone@else.com"})
        refs = {o["order_reference"] for o in response.data["orders"]}
        self.assertNotIn("ZK-OTHER", refs)

    def test_blank_account_email_returns_nothing(self):
        Order.objects.create(order_reference="ZK-NOEMAIL", email="")
        user = User.objects.create_user(username="noemail", email="", password="x-Strong-pass-1")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")

        response = client.get(MY_ORDERS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["orders"], [])

    def test_order_shape_and_items(self):
        order = self.client.get(MY_ORDERS_URL).data["orders"][0]

        self.assertEqual(order["status"], "completed")
        self.assertEqual(order["status_label"], "Completed")
        self.assertEqual(order["items"], [{"name": "30-day eSIM", "sim_type": "esim", "quantity": 1,
                                           "data_allowance": "20GB", "duration": 30}])
        self.assertEqual(len(order["sims"]), 2)

    def test_internal_fields_are_not_exposed(self):
        order = self.client.get(MY_ORDERS_URL).data["orders"][0]

        for field in ("errors", "payload", "assigned", "email", "billing_email", "user_id"):
            self.assertNotIn(field, order)
        for sim in order["sims"]:
            for field in ("imsi", "rate_plan", "customer_account", "group", "point_of_sale",
                          "transaction_id", "activation"):
                self.assertNotIn(field, sim)

    def test_esim_block_and_events(self):
        esim, psim = self.client.get(MY_ORDERS_URL).data["orders"][0]["sims"]

        self.assertEqual(esim["sim_type"], "esim")
        self.assertEqual(esim["network_status"], "Active")
        self.assertTrue(esim["esim"]["ready"])
        self.assertEqual(esim["esim"]["lpa"], "LPA:1$smdp.example$ABC")
        self.assertIsNone(esim["esim"]["qr_image_url"])
        times = [e["at"] for e in esim["events"]]
        self.assertEqual(times, sorted(times))
        self.assertEqual({e["type"] for e in esim["events"]},
                         {"ordered", "activated", "qr_sent", "last_update"})

        self.assertEqual(psim["sim_type"], "psim")
        self.assertIsNone(psim["esim"])
        self.assertEqual({e["type"] for e in psim["events"]}, {"ordered"})

    def test_missing_sim_row_falls_back_to_snapshot(self):
        self.psim.delete()

        sims = self.client.get(MY_ORDERS_URL).data["orders"][0]["sims"]

        self.assertEqual(len(sims), 2)
        fallback = sims[1]
        self.assertEqual(fallback["iccid"], "8944122666635921879")
        self.assertEqual(fallback["msisdn"], "+447421115993")
        self.assertIsNone(fallback["esim"])

    def test_existing_by_user_endpoint_unchanged(self):
        response = APIClient().post("/api/v1/sims/orders/by-user/",
                                    {"email": "lamarr@example.com"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("errors", response.data["orders"][0])
