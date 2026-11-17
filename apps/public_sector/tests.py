from decimal import Decimal
from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.plans.models import Plan

from apps.public_sector.admin import PublicSectorRegistrationAdmin, RegistrationAdminForm
from apps.public_sector.models import Nominee, PublicSectorRegistration

User = get_user_model()

SUBMIT_URL = "/api/public-sector-workers/"
STATUS_URL = "/api/public-sector-workers/me/"
NOMINEES_URL = "/api/public-sector-workers/me/nominees/"
PLANS_URL = "/api/public-sector-workers/plans/"


def payload(**overrides):
    data = {
        "full_name": "Pat Worker",
        "email": "pat@example.com",
        "phone": "07123 456789",
        "date_of_birth": "1990-01-01",
        "employer_name": "NHS Trust",
        "job_title": "Nurse",
        "work_email": "pat@nhs.example",
        "department": "Ward 3",
        "agree_terms": True,
        "is_public_sector_worker": True,
        "confirm_accurate": True,
        "nominees": [],
    }
    data.update(overrides)
    return data


def nominee(n):
    return {
        "full_name": f"Friend {chr(64 + n)}",
        "email": f"friend{n}@example.com",
        "phone": f"0770000000{n}",
        "relationship": "Friend",
    }


class Base(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("pat", "pat@example.com", "pw")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def submit(self, **overrides):
        return self.client.post(SUBMIT_URL, payload(**overrides), format="json")


class SubmitTests(Base):

    def test_login_required(self):
        res = APIClient().post(SUBMIT_URL, payload(), format="json")
        self.assertEqual(res.status_code, 401)

    def test_saved_against_user_as_pending(self):
        res = self.submit(status="approved", nominees=[nominee(1)])
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["data"]["status"], "pending")
        registration = PublicSectorRegistration.objects.get()
        self.assertEqual(registration.user, self.user)
        self.assertEqual(registration.status, "pending")
        self.assertEqual(registration.nominees.count(), 1)

    def test_second_open_registration_refused(self):
        self.submit()
        res = self.submit()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "ALREADY_APPLIED")

    def test_can_apply_again_after_rejection(self):
        self.submit()
        PublicSectorRegistration.objects.update(status="rejected")
        self.assertEqual(self.submit().status_code, 201)

    def test_validation_errors_keep_their_shape(self):
        res = self.submit(agree_terms=False)
        self.assertEqual(res.status_code, 400)
        self.assertIn("agree_terms", res.json()["errors"])


class StatusTests(Base):

    def test_login_required(self):
        self.assertEqual(APIClient().get(STATUS_URL).status_code, 401)

    def test_none_before_applying(self):
        data = self.client.get(STATUS_URL).json()["data"]
        self.assertEqual(data, {"status": "none", "application": None})

    def test_pending_then_approved(self):
        self.submit()
        self.assertEqual(self.client.get(STATUS_URL).json()["data"]["status"], "pending")
        PublicSectorRegistration.objects.update(status="approved")
        self.assertEqual(self.client.get(STATUS_URL).json()["data"]["status"], "approved")

    def test_rejected_shows_reason(self):
        self.submit()
        PublicSectorRegistration.objects.update(status="rejected", rejection_reason="Not eligible")
        data = self.client.get(STATUS_URL).json()["data"]
        self.assertEqual(data["status"], "rejected")
        self.assertEqual(data["application"]["rejection_reason"], "Not eligible")

    def test_other_users_registration_not_visible(self):
        other = User.objects.create_user("other", "other@example.com", "pw")
        PublicSectorRegistration.objects.create(
            user=other, **{k: v for k, v in payload().items() if k != "nominees"}
        )
        self.assertEqual(self.client.get(STATUS_URL).json()["data"]["status"], "none")


class PlansTests(Base):

    def setUp(self):
        super().setUp()
        for name, audience in (
            ("Public 10GB", Plan.Audience.PUBLIC_SECTOR),
            ("Student 10GB", Plan.Audience.STUDENT),
            ("Everyday 10GB", Plan.Audience.GENERAL),
        ):
            Plan.objects.create(
                name=name, price=Decimal("10"), price_24=Decimal("6"), price_12=Decimal("7"),
                price_30=Decimal("8"), duration_days=30, audience=audience,
            )

    def test_anyone_sees_public_sector_plans_without_purchase(self):
        data = APIClient().get(PLANS_URL).json()["data"]
        self.assertEqual(data["status"], "none")
        self.assertFalse(data["can_purchase"])
        self.assertEqual([p["name"] for p in data["plans"]], ["Public 10GB"])
        self.assertEqual(data["plans"][0]["price_24"], "6.00")

    def test_pending_cannot_purchase(self):
        self.submit()
        self.assertFalse(self.client.get(PLANS_URL).json()["data"]["can_purchase"])

    def test_approved_can_purchase(self):
        self.submit()
        PublicSectorRegistration.objects.update(status="approved")
        data = self.client.get(PLANS_URL).json()["data"]
        self.assertEqual(data["status"], "approved")
        self.assertTrue(data["can_purchase"])


class NomineeTests(Base):

    def test_no_registration(self):
        self.assertEqual(self.client.get(NOMINEES_URL).status_code, 404)
        self.assertEqual(self.client.post(NOMINEES_URL, nominee(1), format="json").status_code, 404)

    def test_counts_nominees_from_the_form(self):
        self.submit(nominees=[nominee(1), nominee(2)])
        data = self.client.get(NOMINEES_URL).json()["data"]
        self.assertEqual((data["used"], data["limit"], data["remaining"]), (2, 5, 3))
        self.assertTrue(data["can_add"])
        self.assertEqual(len(data["nominees"]), 2)

    def test_add_up_to_five(self):
        self.submit(nominees=[nominee(1), nominee(2), nominee(3), nominee(4)])
        res = self.client.post(NOMINEES_URL, nominee(5), format="json")
        self.assertEqual(res.status_code, 201)
        data = res.json()["data"]
        self.assertEqual((data["used"], data["remaining"]), (5, 0))
        self.assertFalse(data["can_add"])

        res = self.client.post(NOMINEES_URL, nominee(6), format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "NOMINEE_LIMIT_REACHED")
        self.assertEqual(Nominee.objects.count(), 5)

    def test_duplicates_refused(self):
        self.submit(nominees=[nominee(1)])
        res = self.client.post(NOMINEES_URL, nominee(1), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(set(res.json()["errors"]), {"email", "phone"})

        res = self.client.post(
            NOMINEES_URL, {**nominee(2), "email": "PAT@example.com"}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("email", res.json()["errors"])

    def test_allowed_after_approval_not_after_rejection(self):
        self.submit()
        PublicSectorRegistration.objects.update(status="approved")
        self.assertEqual(self.client.post(NOMINEES_URL, nominee(1), format="json").status_code, 201)

        PublicSectorRegistration.objects.update(status="rejected")
        res = self.client.post(NOMINEES_URL, nominee(2), format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "REGISTRATION_CLOSED")

    def test_invalid_nominee(self):
        self.submit()
        res = self.client.post(NOMINEES_URL, {**nominee(1), "phone": "12"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("phone", res.json()["errors"])


class AdminTests(Base):

    def setUp(self):
        super().setUp()
        self.submit()
        self.registration = PublicSectorRegistration.objects.get()
        self.admin = PublicSectorRegistrationAdmin(PublicSectorRegistration, AdminSite())

    def decide(self, status, reason=""):
        self.registration.status = status
        self.registration.rejection_reason = reason
        form = mock.Mock(changed_data=["status"])
        with mock.patch("apps.public_sector.admin.threading.Thread") as thread, \
                self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(mock.Mock(), self.registration, form, change=True)
        return thread

    def test_decision_sets_reviewed_at_and_emails(self):
        thread = self.decide("approved")
        self.registration.refresh_from_db()
        self.assertIsNotNone(self.registration.reviewed_at)
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["args"], [self.registration.pk])

    def test_back_to_pending_clears_reviewed_at_without_email(self):
        self.decide("rejected", "Missing proof")
        thread = self.decide("pending")
        self.registration.refresh_from_db()
        self.assertIsNone(self.registration.reviewed_at)
        thread.assert_not_called()

    def test_list_view_cannot_reopen_a_second_registration(self):
        PublicSectorRegistration.objects.update(status="rejected")
        self.submit()  # new pending one
        old = PublicSectorRegistration.objects.order_by("created_at", "pk").first()
        form_class = self.admin.get_changelist_form(mock.Mock(), fields=["status"])
        form = form_class(data={"status": "approved"}, instance=old)
        self.assertFalse(form.is_valid())

    def test_full_form_reports_constraint(self):
        PublicSectorRegistration.objects.update(status="rejected")
        self.submit()
        old = PublicSectorRegistration.objects.order_by("created_at", "pk").first()
        data = {k: v for k, v in payload().items() if k != "nominees"}
        form = RegistrationAdminForm(
            data={**data, "user": self.user.pk, "status": "approved", "rejection_reason": ""},
            instance=old,
        )
        self.assertFalse(form.is_valid())
