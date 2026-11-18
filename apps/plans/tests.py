from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.add_line.services.quote import build_quote
from apps.public_sector.models import PublicSectorRegistration
from apps.student_discount.models import StudentDiscountApplication

from apps.plans.eligibility import apply_discount, discounted_prices, link_applications_by_email
from apps.plans.models import Plan, PlanCategory

User = get_user_model()


def make_plan(name, audience=Plan.Audience.GENERAL, **kwargs):
    return Plan.objects.create(
        name=name, price=Decimal("10.00"), price_30=Decimal("10.00"),
        price_12=Decimal("9.00"), price_24=Decimal("8.00"), duration_days=30,
        transatelID="TSL_10", audience=audience, **kwargs,
    )


def make_registration(user=None, status="pending", email="worker@example.com"):
    return PublicSectorRegistration.objects.create(
        user=user, full_name="Pat Worker", email=email, phone="07123 456789",
        date_of_birth="1990-01-01", employer_name="NHS", job_title="Nurse",
        work_email="pat@nhs.example", department="Ward", agree_terms=True,
        is_public_sector_worker=True, confirm_accurate=True, status=status,
    )


def make_application(user=None, status="pending", email="student@example.com",
                     graduation_date=None):
    return StudentDiscountApplication.objects.create(
        user=user, full_name="Sam Student", dob="2004-01-01", email=email,
        mobile="07123456789", institution="Uni", student_id_number="S1",
        enrolment_status="Full time",
        graduation_date=graduation_date or timezone.localdate() + timedelta(days=365),
        selected_plan="Student 10GB", contract_duration="12 months",
        student_id_document="student-discount/test.pdf", signature="Sam",
        declaration_date=timezone.localdate(), status=status,
    )


class PublicPlanListingTests(TestCase):
    """Verified-only plans must not leak into the public plan pages or search."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.category = PlanCategory.objects.create(name="SIM only")
        self.general = make_plan("Everyday 10GB", category=self.category)
        self.student = make_plan("Student 10GB", Plan.Audience.STUDENT, category=self.category)

    def test_existing_plans_default_to_general(self):
        plan = Plan.objects.create(name="Old", price=Decimal("5"), duration_days=30)
        self.assertEqual(plan.audience, Plan.Audience.GENERAL)

    def test_list_only_has_general_plans(self):
        names = [p["name"] for p in self.client.get("/api/plans/v1/").json()]
        self.assertEqual(names, ["Everyday 10GB"])

    def test_category_only_has_general_plans(self):
        res = self.client.get(f"/api/plans/v1/category/{self.category.slug}/")
        self.assertEqual([p["name"] for p in res.json()], ["Everyday 10GB"])

    def test_detail_hides_verified_only_plans(self):
        self.assertEqual(self.client.get(f"/api/plans/v1/{self.general.slug}/").status_code, 200)
        self.assertEqual(self.client.get(f"/api/plans/v1/{self.student.slug}/").status_code, 404)

    def test_search_only_has_active_general_plans(self):
        make_plan("Inactive 10GB", is_active=False)
        res = self.client.get("/search/v1/api/search", {"key": "10GB"})
        titles = [r["title"] for r in res.json().get("data", []) if r["type"] == "plan"]
        self.assertEqual(titles, ["Everyday 10GB"])


class DiscountHelperTests(TestCase):

    def test_apply_discount_rounds_half_up_to_the_penny(self):
        self.assertEqual(apply_discount("16.99", 30), Decimal("11.89"))   # 11.893
        self.assertEqual(apply_discount("28.34", 30), Decimal("19.84"))   # 19.838
        self.assertEqual(apply_discount("12.99", 20), Decimal("10.39"))   # 10.392
        self.assertEqual(apply_discount("10.05", 50), Decimal("5.03"))    # 5.025 → up

    def test_discounted_prices_keeps_empty_terms_empty(self):
        data = {"price": "20.00", "price_24": "15.00", "price_12": None, "price_30": "18.00"}
        self.assertEqual(discounted_prices(data, 20), {
            "price_24": "12.00", "price_12": None, "price_30": "14.40", "price": "16.00",
        })


class QuoteTests(TestCase):
    """Add-a-line and Switch & Save: every active plan at its normal price."""

    def setUp(self):
        self.user = User.objects.create_user("buyer", "buyer@example.com", "pw")
        self.student = make_plan("Student", Plan.Audience.STUDENT)

    def test_anyone_can_quote_a_student_plan_at_normal_price(self):
        self.assertEqual(
            build_quote(None, self.student.pk, "12M", "esim", additional_line=False).monthly_price,
            Decimal("9.00"),
        )
        self.assertEqual(build_quote(self.user, self.student.pk, "12M", "esim").monthly_price, Decimal("9.00"))

    def test_no_verified_discount_in_quotes(self):
        make_application(self.user, status="approved")
        self.assertEqual(build_quote(self.user, self.student.pk, "12M", "esim").monthly_price, Decimal("9.00"))

    def test_general_plan_unchanged(self):
        general = make_plan("Everyday")
        self.assertEqual(build_quote(None, general.pk, "24M", "esim").monthly_price, Decimal("8.00"))


class LinkByEmailTests(TestCase):

    def test_links_unique_active_account_ignoring_case(self):
        user = User.objects.create_user("pat", "Worker@Example.com", "pw")
        registration = make_registration(email="worker@example.com")
        self.assertEqual(
            link_applications_by_email(PublicSectorRegistration, User, ("pending", "approved")),
            (1, 0),
        )
        registration.refresh_from_db()
        self.assertEqual(registration.user, user)

    def test_skips_ambiguous_and_inactive_accounts(self):
        User.objects.create_user("a", "same@example.com", "pw")
        User.objects.create_user("b", "same@example.com", "pw")
        User.objects.create_user("c", "inactive@example.com", "pw", is_active=False)
        make_registration(email="same@example.com")
        make_registration(email="inactive@example.com")
        self.assertEqual(
            link_applications_by_email(PublicSectorRegistration, User, ("pending", "approved")),
            (0, 2),
        )

    def test_only_newest_open_application_is_linked(self):
        user = User.objects.create_user("sam", "student@example.com", "pw")
        older = make_application(status="pending")
        newer = make_application(status="under_review")
        rejected = make_application(status="rejected")
        open_statuses = StudentDiscountApplication.OPEN_STATUSES
        self.assertEqual(
            link_applications_by_email(StudentDiscountApplication, User, open_statuses), (2, 1)
        )
        for row in (older, newer, rejected):
            row.refresh_from_db()
        self.assertIsNone(older.user)
        self.assertEqual(newer.user, user)
        self.assertEqual(rejected.user, user)
