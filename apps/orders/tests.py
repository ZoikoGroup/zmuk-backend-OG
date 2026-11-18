from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.coupons.models import Coupon, CouponUsage
from apps.orders.models import CheckoutPayment
from apps.orders.views import _process_checkout_payment
from apps.plans.models import Plan
from apps.products.models import Product, ProductVariant
from apps.public_sector.models import PublicSectorOffer, PublicSectorRegistration
from apps.student_discount.models import StudentDiscountApplication

User = get_user_model()

URL = "/api/v1/checkout/create-intent/"


def make_plan(name="Everyday 10GB", audience=Plan.Audience.GENERAL, **kwargs):
    defaults = dict(
        price=Decimal("15.00"), price_24=Decimal("8.00"), price_12=Decimal("10.00"),
        price_30=Decimal("12.00"), duration_days=30, transatelID="TSL_10",
    )
    return Plan.objects.create(name=name, audience=audience, **{**defaults, **kwargs})


def plan_line(plan, duration=730, price="8.00", quantity=1, sim="esim"):
    """A cart line as app/checkout/page.tsx sends it (normalised item + _raw)."""
    return {
        "cartKey": f"plan-{plan.id}-{duration}-{sim}",
        "id": plan.id,
        "title": plan.name,
        "price": float(price),
        "quantity": quantity,
        "category": "SIM Only Plans",
        "image": "",
        "simType": "eSIM" if sim == "esim" else "pSIM",
        "simTypeKey": sim,
        "duration": duration,
        "dataAllowance": "10GB",
        "transatelID": "BROWSER_VALUE",
        "tier": None,
        "_raw": {
            "cartKey": f"plan-{plan.id}-{duration}-{sim}", "id": plan.id, "type": "plan",
            "name": plan.name, "price": float(price), "quantity": quantity,
            "metadata": {"simType": sim, "duration": duration, "transatelID": "BROWSER_VALUE"},
        },
    }


def product_line(variant, price, quantity=1):
    return {
        "cartKey": f"product-{variant.id}", "id": variant.id, "title": "Phone",
        "price": float(price), "quantity": quantity, "simType": "pSIM", "simTypeKey": "psim",
        "duration": None, "transatelID": "",
        "_raw": {"id": variant.id, "type": "product", "price": float(price), "quantity": quantity},
    }


def body(cart, total, **extra):
    return {
        "cart": cart, "total": total, "subtotal": total, "discountAmount": 0,
        "billingAddress": {"email": "buyer@example.com", "firstName": "Bo", "lastName": "Buyer"},
        "shippingAddress": {}, **extra,
    }


@override_settings(STRIPE_SECRET_KEY="sk_test_dummy")
class Base(TestCase):

    def setUp(self):
        cache.clear()
        patcher = mock.patch(
            "apps.orders.views.stripe.PaymentIntent.create",
            return_value=SimpleNamespace(id="pi_123", client_secret="cs_123"),
        )
        self.stripe_create = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = APIClient()
        self.user = User.objects.create_user("buyer", "buyer@example.com", "pw")
        self.plan = make_plan()

    def post(self, data, login=False):
        if login:
            self.client.force_authenticate(self.user)
        return self.client.post(URL, data, format="json")


class PricingTests(Base):

    def test_guest_buys_general_plan_at_server_price(self):
        res = self.post(body([plan_line(self.plan, 730, "8.00", quantity=2)], 16))
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["clientSecret"], "cs_123")
        self.assertEqual(res.json()["totals"]["total"], "16.00")

        payment = CheckoutPayment.objects.get()
        self.assertEqual(payment.amount_pence, 1600)
        self.assertIsNone(payment.user)
        self.assertEqual(self.stripe_create.call_args.kwargs["amount"], 1600)
        line = payment.payload["cart"][0]
        self.assertEqual((line["price"], line["totalPrice"], line["plan_id"]), (8.0, 16.0, self.plan.id))
        # Transatel package from our plan, not from the browser.
        self.assertEqual(line["transatelID"], "TSL_10")

    def test_term_prices(self):
        for duration, price in ((730, "8.00"), (365, "10.00"), (30, "12.00"), (28, "15.00"), (None, "15.00")):
            with self.subTest(duration=duration):
                res = self.post(body([plan_line(self.plan, duration, price)], price))
                self.assertEqual(res.status_code, 200, res.content)

    def test_empty_term_price_falls_back_to_price(self):
        plan = make_plan("No 24", price_24=None)
        self.assertEqual(self.post(body([plan_line(plan, 730, "15.00")], 15)).status_code, 200)

    def test_sale_price_from_browser_is_refused(self):
        # What the five plan pages send today: final_price with the 24-month tab shown.
        res = self.post(body([plan_line(self.plan, 30, "15.00")], 15))
        self.assertEqual(res.status_code, 409)
        data = res.json()
        self.assertEqual(data["code"], "PRICE_CHANGED")
        self.assertEqual(data["totals"]["total"], "12.00")
        self.assertEqual(data["lines"][0]["unit_price"], "12.00")
        self.assertFalse(CheckoutPayment.objects.exists())
        self.stripe_create.assert_not_called()

    def test_tampered_total_is_refused(self):
        res = self.post(body([plan_line(self.plan, 730, "1.00")], 1))
        self.assertEqual(res.json()["code"], "PRICE_CHANGED")
        self.assertFalse(CheckoutPayment.objects.exists())

    def test_unknown_or_inactive_plan(self):
        inactive = make_plan("Old", is_active=False)
        for plan in (inactive, SimpleNamespace(id=99999, name="Ghost")):
            with self.subTest(plan=plan.name):
                res = self.post(body([plan_line(plan, 730, "8.00")], 8))
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.json()["code"], "PLAN_NOT_AVAILABLE")

    def test_invalid_requests(self):
        self.assertEqual(self.post(body([plan_line(self.plan)], 0)).json()["code"], "TOTAL_INVALID")
        self.assertEqual(self.post(body([], 8)).json()["code"], "CART_EMPTY")
        res = self.post(body([plan_line(self.plan, quantity=0)], 8))
        self.assertEqual(res.json()["code"], "CART_INVALID")

    def test_products_priced_from_variant(self):
        product = Product.objects.create(name="Phone", slug="phone")
        variant = ProductVariant.objects.create(product=product, price=Decimal("199.00"))
        cart = [plan_line(self.plan, 365, "10.00"), product_line(variant, "199.00")]
        res = self.post(body(cart, "209.00"))
        self.assertEqual(res.status_code, 200, res.content)

        res = self.post(body([product_line(variant, "99.00")], 99))
        self.assertEqual(res.json()["code"], "PRICE_CHANGED")

        variant.is_active = False
        variant.save()
        res = self.post(body([product_line(variant, "199.00")], 199))
        self.assertEqual(res.json()["code"], "PRODUCT_NOT_AVAILABLE")


class EligibilityTests(Base):

    def setUp(self):
        super().setUp()
        self.student_plan = make_plan("Student 10GB", Plan.Audience.STUDENT, price_24=Decimal("5.00"))

    def approve_student(self, graduation_date=None):
        return StudentDiscountApplication.objects.create(
            user=self.user, full_name="Bo", dob="2004-01-01", email="buyer@example.com",
            mobile="07123456789", institution="Uni", student_id_number="S1",
            enrolment_status="Full time",
            graduation_date=graduation_date or timezone.localdate() + timedelta(days=365),
            selected_plan="Student", contract_duration="24 months",
            student_id_document="student-discount/x.pdf", signature="Bo",
            declaration_date=timezone.localdate(), status="approved",
        )

    def test_guest_must_log_in(self):
        res = self.post(body([plan_line(self.student_plan, 730, "5.00")], 5))
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["code"], "LOGIN_REQUIRED")

    def test_not_approved_is_refused(self):
        res = self.post(body([plan_line(self.student_plan, 730, "5.00")], 5), login=True)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "PLAN_NOT_ELIGIBLE")
        self.assertIn("verified students", res.json()["message"])

    def test_approved_student_can_buy(self):
        self.approve_student()
        res = self.post(body([plan_line(self.student_plan, 730, "5.00")], 5), login=True)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(CheckoutPayment.objects.get().user, self.user)

    def test_expired_student_is_refused(self):
        self.approve_student(graduation_date=timezone.localdate() - timedelta(days=1))
        res = self.post(body([plan_line(self.student_plan, 730, "5.00")], 5), login=True)
        self.assertEqual(res.json()["code"], "PLAN_NOT_ELIGIBLE")

    def test_public_sector_approval_does_not_unlock_student_plans(self):
        make_registration(self.user, "approved")
        res = self.post(body([plan_line(self.student_plan, 730, "5.00")], 5), login=True)
        self.assertEqual(res.json()["code"], "PLAN_NOT_ELIGIBLE")


def make_registration(user, status):
    return PublicSectorRegistration.objects.create(
        user=user, full_name="Bo", email="buyer@example.com", phone="07123 456789",
        date_of_birth="1990-01-01", employer_name="NHS", job_title="Nurse",
        work_email="bo@nhs.example", department="Ward", agree_terms=True,
        is_public_sector_worker=True, confirm_accurate=True, status=status,
    )


class PublicSectorDiscountTests(Base):
    """30% off offer plans for approved workers, automatically, no coupon on top."""

    def setUp(self):
        super().setUp()
        self.offer_plan = make_plan(
            "Max 30GB", price=Decimal("30.00"), price_24=Decimal("16.99"),
            price_12=Decimal("28.34"), price_30=Decimal("29.50"),
        )
        self.offer = PublicSectorOffer.objects.get()  # from the migration: 30%, active
        self.offer.plans.add(self.offer_plan)

    def test_approved_worker_pays_30_percent_less(self):
        make_registration(self.user, "approved")
        res = self.post(body([plan_line(self.offer_plan, 730, "11.89")], "11.89"), login=True)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["totals"]["public_sector_discount"], "5.10")

        payment = CheckoutPayment.objects.get()
        self.assertEqual(payment.amount_pence, 1189)
        line = payment.payload["cart"][0]
        self.assertEqual((line["price"], line["list_price"], line["discount_percent"]),
                         (11.89, 16.99, "30.00"))

    def test_rounding_is_per_unit_then_times_quantity(self):
        make_registration(self.user, "approved")
        res = self.post(body([plan_line(self.offer_plan, 365, "19.84", quantity=2)], "39.68"), login=True)
        self.assertEqual(res.status_code, 200, res.content)

    def test_approved_worker_sending_normal_price_gets_price_changed(self):
        make_registration(self.user, "approved")
        res = self.post(body([plan_line(self.offer_plan, 730, "16.99")], "16.99"), login=True)
        self.assertEqual(res.json()["code"], "PRICE_CHANGED")
        line = res.json()["lines"][0]
        self.assertEqual((line["list_price"], line["discount_percent"], line["unit_price"]),
                         ("16.99", "30.00", "11.89"))

    def test_no_discount_for_guest_pending_or_paused_offer(self):
        cases = [("guest", None, False), ("pending", "pending", True), ("rejected", "rejected", True)]
        for label, status, login in cases:
            with self.subTest(label):
                PublicSectorRegistration.objects.all().delete()
                if status:
                    make_registration(self.user, status)
                res = self.post(body([plan_line(self.offer_plan, 730, "16.99")], "16.99"), login=login)
                self.assertEqual(res.status_code, 200, res.content)

        PublicSectorRegistration.objects.all().delete()
        make_registration(self.user, "approved")
        self.offer.is_active = False
        self.offer.save()
        res = self.post(body([plan_line(self.offer_plan, 730, "16.99")], "16.99"), login=True)
        self.assertEqual(res.status_code, 200, res.content)

    def test_plan_outside_the_offer_keeps_its_price(self):
        make_registration(self.user, "approved")
        res = self.post(body([plan_line(self.plan, 730, "8.00")], "8.00"), login=True)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["totals"]["public_sector_discount"], "0.00")

    def test_coupon_not_combinable_with_the_discount(self):
        Coupon.objects.create(name="SAVE10", slug="SAVE10", type=Coupon.PERCENTAGE, discount=Decimal("10"))
        make_registration(self.user, "approved")
        res = self.post(
            body([plan_line(self.offer_plan, 730, "11.89")], "10.70", couponCode="SAVE10"), login=True
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "COUPON_NOT_COMBINABLE")

        # A cart with no discounted plan can still use the coupon.
        res = self.post(body([plan_line(self.plan, 730, "8.00")], "7.20", couponCode="SAVE10"), login=True)
        self.assertEqual(res.status_code, 200, res.content)


class CouponTests(Base):

    def make_coupon(self, slug="SAVE10", **kwargs):
        defaults = dict(name=slug, type=Coupon.PERCENTAGE, discount=Decimal("10.00"))
        return Coupon.objects.create(slug=slug, **{**defaults, **kwargs})

    def test_percentage_on_whole_cart(self):
        self.make_coupon()
        other = make_plan("Big 50GB", price_12=Decimal("20.00"))
        cart = [plan_line(self.plan, 730, "8.00"), plan_line(other, 365, "20.00")]
        res = self.post(body(cart, "25.20", couponCode="save10"), login=True)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["totals"], {
            "subtotal": "28.00", "discount": "2.80", "public_sector_discount": "0.00",
            "shipping": "0.00", "total": "25.20",
        })
        self.assertEqual(CheckoutPayment.objects.get().payload["coupon"]["code"], "SAVE10")

    def test_browser_discount_is_ignored(self):
        # No coupon code sent: a discount claimed by the browser doesn't count.
        res = self.post(body([plan_line(self.plan, 730, "8.00")], "4.00", discountAmount=4), login=True)
        self.assertEqual(res.json()["code"], "PRICE_CHANGED")

    def test_coupon_needs_login(self):
        self.make_coupon()
        res = self.post(body([plan_line(self.plan, 730, "8.00")], "7.20", couponCode="SAVE10"))
        self.assertEqual(res.json()["code"], "LOGIN_REQUIRED")

    def test_unknown_inactive_and_user_restricted_coupons(self):
        self.make_coupon("OFF", status=Coupon.INACTIVE)
        mine = self.make_coupon("MINE")
        mine.users.add(User.objects.create_user("other", "other@example.com", "pw"))
        for code in ("NOPE", "OFF", "MINE"):
            with self.subTest(code=code):
                res = self.post(body([plan_line(self.plan, 730, "8.00")], "7.20", couponCode=code), login=True)
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.json()["code"], "COUPON_INVALID")

    def test_plan_restricted_coupon(self):
        coupon = self.make_coupon("PLANONLY")
        coupon.plans.add(self.plan)
        other = make_plan("Other")
        ok = self.post(body([plan_line(self.plan, 730, "8.00")], "7.20", couponCode="PLANONLY"), login=True)
        self.assertEqual(ok.status_code, 200, ok.content)

        cart = [plan_line(self.plan, 730, "8.00"), plan_line(other, 730, "8.00")]
        res = self.post(body(cart, "14.40", couponCode="PLANONLY"), login=True)
        self.assertEqual(res.json()["code"], "COUPON_INVALID")

    def test_usage_recorded_after_payment_then_once_per_customer(self):
        coupon = self.make_coupon(limit=5)
        res = self.post(body([plan_line(self.plan, 730, "8.00")], "7.20", couponCode="SAVE10"), login=True)
        self.assertEqual(res.status_code, 200, res.content)
        payment = CheckoutPayment.objects.get()
        self.assertFalse(CouponUsage.objects.exists())  # nothing used until paid

        with mock.patch("apps.orders.views._send_checkout_receipt"), \
                mock.patch("apps.orders.views._notify_team_of_checkout"):
            _process_checkout_payment(payment)
            _process_checkout_payment(payment)  # repeat (webhook + confirm) counts once

        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)
        self.assertEqual(CouponUsage.objects.get().user, self.user)

        res = self.post(body([plan_line(self.plan, 730, "8.00")], "7.20", couponCode="SAVE10"), login=True)
        self.assertEqual(res.json()["code"], "COUPON_INVALID")
