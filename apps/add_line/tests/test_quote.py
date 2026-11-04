from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core import signing
from django.test import TestCase, override_settings

from apps.add_line.services.quote import QuoteError, build_quote, issue_token, redeem_token
from apps.coupons.models import Coupon
from apps.plans.models import Plan


def make_plan(**kwargs):
    defaults = dict(
        name="Standard 10GB", price=Decimal("12.00"), price_30=Decimal("10.00"),
        price_12=Decimal("9.00"), price_24=Decimal("8.00"), duration_days=30,
        transatelID="TSL_UK_10GB",
    )
    return Plan.objects.create(**{**defaults, **kwargs})


@override_settings(ADD_LINE_QUOTE_MINUTES=30)
class QuoteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.plan = make_plan()

    def test_term_prices_match_storefront(self):
        for term, expected in (("30D", "10.00"), ("12M", "9.00"), ("24M", "8.00")):
            quote = build_quote(self.user, self.plan.pk, term, "esim")
            self.assertEqual(quote.as_dict()["monthly_price"], expected)

    def test_missing_term_price_falls_back_to_price(self):
        plan = make_plan(name="Other", price_12=None)
        self.assertEqual(build_quote(self.user, plan.pk, "12M", "esim").monthly_price, Decimal("12.00"))

    def test_vat_is_included_not_added(self):
        quote = build_quote(self.user, self.plan.pk, "30D", "esim").as_dict()
        self.assertEqual(quote["charge_today"], "10.00")
        self.assertEqual(quote["vat_included"], "1.67")
        self.assertEqual(quote["delivery_fee"], "0.00")

    def test_first_renewal_date(self):
        with mock.patch("apps.add_line.services.quote.timezone.localdate", return_value=date(2026, 1, 31)):
            self.assertEqual(build_quote(self.user, self.plan.pk, "30D", "esim").first_renewal, date(2026, 3, 2))
            self.assertEqual(build_quote(self.user, self.plan.pk, "12M", "esim").first_renewal, date(2026, 2, 28))

    def test_inactive_or_unactivatable_plan_rejected(self):
        for plan in (make_plan(name="Old", is_active=False), make_plan(name="NoCode", transatelID="")):
            with self.assertRaises(QuoteError) as ctx:
                build_quote(self.user, plan.pk, "30D", "esim")
            self.assertEqual(ctx.exception.code, "PLAN_NOT_AVAILABLE")

    def test_acquisition_coupon_rejected_for_additional_line(self):
        Coupon.objects.create(name="WELCOME", slug="welcome", discount=Decimal("5.00"))
        with self.assertRaises(QuoteError) as ctx:
            build_quote(self.user, self.plan.pk, "30D", "esim", "WELCOME")
        self.assertEqual(ctx.exception.code, "PROMO_NOT_ELIGIBLE")

    def test_additional_line_coupon_applies(self):
        Coupon.objects.create(name="EXTRA", slug="extra", discount=Decimal("2.50"),
                              valid_for_additional_lines=True)
        quote = build_quote(self.user, self.plan.pk, "30D", "esim", "extra").as_dict()
        self.assertEqual(quote["charge_today"], "7.50")
        self.assertEqual(quote["promo"], {"code": "extra", "discount": "2.50"})
        self.assertEqual(quote["recurring_amount"], "10.00")

    def test_unknown_coupon(self):
        with self.assertRaises(QuoteError) as ctx:
            build_quote(self.user, self.plan.pk, "30D", "esim", "NOPE")
        self.assertEqual(ctx.exception.code, "PROMO_NOT_FOUND")

    def test_token_round_trip(self):
        quote = build_quote(self.user, self.plan.pk, "12M", "psim")
        again = redeem_token(self.user, issue_token(self.user, quote))
        self.assertEqual(again.hash, quote.hash)

    def test_token_bound_to_user(self):
        other = User.objects.create_user("bob", "bob@example.com", "pw")
        token = issue_token(self.user, build_quote(self.user, self.plan.pk, "30D", "esim"))
        with self.assertRaises(QuoteError) as ctx:
            redeem_token(other, token)
        self.assertEqual(ctx.exception.code, "QUOTE_INVALID")

    def test_price_change_invalidates_token(self):
        token = issue_token(self.user, build_quote(self.user, self.plan.pk, "30D", "esim"))
        Plan.objects.filter(pk=self.plan.pk).update(price_30=Decimal("11.00"))
        with self.assertRaises(QuoteError) as ctx:
            redeem_token(self.user, token)
        self.assertEqual(ctx.exception.code, "QUOTE_CHANGED")

    def test_tampered_or_expired_token(self):
        token = issue_token(self.user, build_quote(self.user, self.plan.pk, "30D", "esim"))
        with self.assertRaises(QuoteError) as ctx:
            redeem_token(self.user, token[:-2] + "xx")
        self.assertEqual(ctx.exception.code, "QUOTE_INVALID")
        with mock.patch("apps.add_line.services.quote.signing.loads", side_effect=signing.SignatureExpired):
            with self.assertRaises(QuoteError) as ctx:
                redeem_token(self.user, token)
        self.assertEqual(ctx.exception.code, "QUOTE_EXPIRED")

    def test_quote_endpoint(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.user)
        resp = client.post("/api/v1/lines/quote/", {"plan_id": self.plan.pk, "term": "30D", "sim_type": "esim"},
                           format="json")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["quote"]["charge_today"], "10.00")
        self.assertTrue(body["quote_token"])
        self.assertEqual(body["expires_in_seconds"], 1800)
        self.assertEqual(set(body["contract"]), {"contract_version", "contract_summary_version"})
