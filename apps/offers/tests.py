from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.coupons.models import Coupon
from apps.offers.admin import OfferAdminForm
from apps.offers.models import Offer
from apps.offers.services import live_offers

User = get_user_model()

URL = "/api/v1/offers/"


def make_coupon(**overrides):
    data = {"name": "BF30", "slug": "bf30", "discount": Decimal("30.00"), "type": Coupon.PERCENTAGE}
    data.update(overrides)
    return Coupon.objects.create(**data)


class Base(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.client = APIClient()
        self.coupon = make_coupon()

    def offer(self, **overrides):
        data = {
            "slug": f"offer-{Offer.objects.count() + 1}",
            "title": "Black Friday",
            "subtitle": "30% off selected plans",
            "bullets": ["30% off", "Any SIM plan"],
            "terms_text": "One use per customer.",
            "mechanism": Offer.Mechanism.COUPON,
            "coupon": self.coupon,
            "starts_at": self.now - timedelta(days=1),
            "ends_at": self.now + timedelta(days=6),
            "is_published": True,
        }
        data.update(overrides)
        return Offer.objects.create(**data)

    def slugs(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        return [card["slug"] for card in response.json()["results"]]


class OfferListTests(Base):
    def test_card_shape_comes_from_the_coupon(self):
        self.offer(slug="bf", cta_label="Shop now", cta_href="/plans", priority=1)

        body = self.client.get(URL).json()

        self.assertIn("server_time", body)
        card = body["results"][0]
        self.assertEqual(card["slug"], "bf")
        self.assertEqual(card["mechanism"], "coupon")
        self.assertEqual(card["badge"], "Limited time")
        self.assertEqual(card["bullets"], ["30% off", "Any SIM plan"])
        self.assertEqual(card["discount_type"], "percentage")
        self.assertEqual(card["discount_value"], "30.00")
        self.assertEqual(card["coupon_code"], "bf30")
        self.assertIs(card["combinable"], False)
        self.assertEqual(card["cta_href"], "/plans")
        self.assertEqual(card["priority"], 1)

    def test_info_offer_has_no_code_or_discount(self):
        self.offer(mechanism=Offer.Mechanism.INFO, coupon=None)

        card = self.client.get(URL).json()["results"][0]

        self.assertEqual(card["coupon_code"], "")
        self.assertIsNone(card["discount_type"])
        self.assertIsNone(card["discount_value"])

    def test_only_published_offers_inside_their_times(self):
        self.offer(slug="live")
        self.offer(slug="draft", is_published=False)
        self.offer(slug="future", starts_at=self.now + timedelta(hours=1), ends_at=self.now + timedelta(days=2))
        self.offer(slug="ended", starts_at=self.now - timedelta(days=3), ends_at=self.now - timedelta(seconds=1))

        self.assertEqual(self.slugs(), ["live"])

    def test_card_disappears_when_the_coupon_stops_working(self):
        cases = {
            "inactive": {"status": Coupon.INACTIVE},
            "expired": {"valid_till": self.now - timedelta(minutes=1)},
            "used-up": {"limit": 100, "used_count": 100},
        }
        for slug, fields in cases.items():
            coupon = make_coupon(name=slug, slug=slug, **fields)
            self.offer(slug=slug, coupon=coupon)
        self.offer(slug="ok")

        self.assertEqual(self.slugs(), ["ok"])

    def test_coupon_assigned_to_users_after_saving_is_never_shown(self):
        self.offer(slug="bf")
        self.coupon.users.add(User.objects.create_user("vip", "vip@example.com", "pw"))

        self.assertEqual(self.slugs(), [])

    def test_order_is_priority_then_soonest_ending(self):
        self.offer(slug="later", priority=5, ends_at=self.now + timedelta(days=9))
        self.offer(slug="sooner", priority=5, ends_at=self.now + timedelta(days=2))
        self.offer(slug="top", priority=1)

        self.assertEqual(self.slugs(), ["top", "sooner", "later"])

    def test_no_login_needed_and_a_stale_token_is_ignored(self):
        self.offer(slug="bf")
        self.client.credentials(HTTP_AUTHORIZATION="Token not-a-real-token")

        response = self.client.get(URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 1)

    def test_short_public_cache(self):
        response = self.client.get(URL)

        self.assertEqual(response["Cache-Control"], "public, max-age=60")

    def test_read_only(self):
        self.assertEqual(self.client.post(URL, {}).status_code, 405)

    def test_query_count_does_not_grow_with_offers(self):
        for i in range(5):
            coupon = make_coupon(name=f"C{i}", slug=f"c{i}")
            self.offer(slug=f"o{i}", coupon=coupon)

        with self.assertNumQueries(2):  # offers + coupons' users
            self.assertEqual(len(live_offers()), 5)


class OfferValidationTests(Base):
    def build(self, **overrides):
        offer = Offer(
            slug="x",
            title="X",
            mechanism=Offer.Mechanism.COUPON,
            coupon=self.coupon,
            starts_at=self.now,
            ends_at=self.now + timedelta(days=1),
        )
        for key, value in overrides.items():
            setattr(offer, key, value)
        return offer

    def assert_invalid(self, field, **overrides):
        with self.assertRaises(ValidationError) as ctx:
            self.build(**overrides).full_clean()
        self.assertIn(field, ctx.exception.message_dict)

    def test_valid_offer_passes(self):
        self.build().full_clean()

    def test_end_must_be_after_start(self):
        self.assert_invalid("ends_at", ends_at=self.now)

    def test_coupon_offer_needs_a_coupon(self):
        self.assert_invalid("coupon", coupon=None)

    def test_info_offer_cannot_have_a_coupon(self):
        self.assert_invalid("coupon", mechanism=Offer.Mechanism.INFO)

    def test_personal_coupon_is_refused(self):
        self.coupon.users.add(User.objects.create_user("vip", "vip@example.com", "pw"))
        self.assert_invalid("coupon")

    def test_offer_must_sit_inside_the_coupon_dates(self):
        self.coupon.valid_from = self.now + timedelta(hours=1)
        self.coupon.valid_till = self.now + timedelta(hours=10)
        self.coupon.save()

        self.assert_invalid("starts_at")
        self.assert_invalid(
            "ends_at", starts_at=self.now + timedelta(hours=2), ends_at=self.now + timedelta(days=1)
        )

    def test_button_link_must_stay_on_the_site(self):
        for link in ("https://evil.example", "//evil.example", "javascript:alert(1)"):
            self.assert_invalid("cta_href", cta_href=link)
        self.build(cta_href="/plans?x=1").full_clean()

    def test_at_most_four_bullets(self):
        self.assert_invalid("bullets", bullets=["a", "b", "c", "d", "e"])


class OfferAdminFormTests(Base):
    def test_bullets_are_typed_one_per_line(self):
        form = OfferAdminForm(data={
            "slug": "bf",
            "title": "Black Friday",
            "badge": "Limited time",
            "bullets": "30% off\n\n  Any SIM plan  \n",
            "mechanism": Offer.Mechanism.COUPON,
            "coupon": self.coupon.pk,
            "starts_at": self.now.strftime("%Y-%m-%d %H:%M:%S"),
            "ends_at": (self.now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "priority": 1,
        })

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["bullets"], ["30% off", "Any SIM plan"])

    def test_saved_bullets_show_one_per_line(self):
        offer = self.offer()

        form = OfferAdminForm(instance=offer)

        self.assertEqual(form.initial["bullets"], "30% off\nAny SIM plan")
