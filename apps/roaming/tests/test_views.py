import io
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .factories import charged_row, included_row, load_register, make_line, make_plan, map_plan


class RoamingApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_roaming_destinations", stdout=io.StringIO())

    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        plan = make_plan()
        map_plan(plan)
        self.line = make_line(self.user, plan)
        load_register([included_row("FR"), charged_row("US", plan_sku="", plan_version="", rating_zone="Zone 4")])
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def quote_url(self, line=None):
        return f"/api/v1/lines/{(line or self.line).uuid}/roaming-quote/"

    def test_quote_endpoint(self):
        resp = self.client.get(self.quote_url(), {"country": "fr"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["outcome"], "INCLUDED_WITH_LIMIT")
        self.assertEqual(body["travel_date"], timezone.localdate().isoformat())
        self.assertEqual(resp["Cache-Control"], "no-store")

    def test_another_customers_line_is_not_found(self):
        other = User.objects.create_user("bob", "bob@example.com", "pw")
        resp = self.client.get(self.quote_url(make_line(other, None)), {"country": "FR"})
        self.assertEqual(resp.status_code, 404)

    def test_login_required(self):
        resp = APIClient().get(self.quote_url(), {"country": "FR"})
        self.assertEqual(resp.status_code, 401)

    def test_bad_parameters(self):
        self.assertEqual(self.client.get(self.quote_url()).status_code, 400)
        resp = self.client.get(self.quote_url(), {"country": "GB"})
        self.assertEqual((resp.status_code, resp.json()["code"]), (400, "DOMESTIC_DESTINATION"))
        resp = self.client.get(self.quote_url(), {"country": "ZZ"})
        self.assertEqual(resp.json()["code"], "UNKNOWN_DESTINATION")
        far = (timezone.localdate() + timedelta(days=400)).isoformat()
        self.assertEqual(self.client.get(self.quote_url(), {"country": "FR", "travel_date": far}).status_code, 400)

    def test_destination_search(self):
        resp = APIClient().get("/api/v1/roaming/destinations/", {"q": "jer"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([d["code"] for d in resp.json()["destinations"]], ["JE"])

        codes = {d["code"]: d for d in APIClient().get("/api/v1/roaming/destinations/").json()["destinations"]}
        self.assertNotIn("GB", codes)
        self.assertEqual(codes["US"]["rating_zone"], "Zone 4")
        self.assertIsNone(codes["FR"]["rating_zone"])  # only published rows give a zone

    def test_search_by_alias(self):
        names = [d["name"] for d in APIClient().get("/api/v1/roaming/destinations/", {"q": "eire"}).json()["destinations"]]
        self.assertEqual(names, ["Ireland"])

    def test_public_destination_rates(self):
        resp = APIClient().get("/api/v1/roaming/destinations/us/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "PUBLIC_RATES_ONLY")
        self.assertEqual(APIClient().get("/api/v1/roaming/destinations/zz/").status_code, 404)
