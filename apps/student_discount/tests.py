import importlib
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.plans.models import Plan

from apps.student_discount.models import StudentDiscountApplication

User = get_user_model()

SUBMIT_URL = "/api/student-discount/"
STATUS_URL = "/api/student-discount/me/"
PLANS_URL = "/api/student-discount/plans/"


def payload(**overrides):
    today = timezone.localdate()
    data = {
        "full_name": "Sam Student",
        "dob": "2004-01-01",
        "email": "sam@example.com",
        "mobile": "07123456789",
        "institution": "University of Leeds",
        "student_id_number": "S12345",
        "enrolment_status": "Full time",
        "graduation_date": (today + timedelta(days=365)).isoformat(),
        "selected_plan": "Student 10GB",
        "contract_duration": "12 months",
        "signature": "Sam Student",
        "declaration_date": today.isoformat(),
        "student_id_document": SimpleUploadedFile(
            "sam-smith-student-card.pdf", b"%PDF-1.4 test", content_type="application/pdf"
        ),
    }
    data.update(overrides)
    return data


class Base(TestCase):
    """Uploads go to temporary folders, never into the real media folders."""

    def setUp(self):
        cache.clear()
        self.private_dir = tempfile.mkdtemp()
        self.public_dir = tempfile.mkdtemp()
        field = StudentDiscountApplication._meta.get_field("student_id_document")
        self._storage = field.storage
        field.storage = FileSystemStorage(location=self.private_dir)
        settings_override = override_settings(MEDIA_ROOT=self.public_dir)
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        self.user = User.objects.create_user("sam", "sam@example.com", "pw")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def tearDown(self):
        StudentDiscountApplication._meta.get_field("student_id_document").storage = self._storage
        shutil.rmtree(self.private_dir, ignore_errors=True)
        shutil.rmtree(self.public_dir, ignore_errors=True)

    def submit(self, **overrides):
        return self.client.post(SUBMIT_URL, payload(**overrides), format="multipart")


class SubmitTests(Base):

    def test_login_required(self):
        res = APIClient().post(SUBMIT_URL, payload(), format="multipart")
        self.assertEqual(res.status_code, 401)

    def test_saved_against_user_and_status_cannot_be_set(self):
        res = self.submit(status="approved")
        self.assertEqual(res.status_code, 201)
        body = res.json()["data"]
        self.assertEqual(body["status"], "pending")
        self.assertNotIn("student_id_document", body)

        application = StudentDiscountApplication.objects.get()
        self.assertEqual(application.user, self.user)
        self.assertEqual(application.status, "pending")

    def test_document_is_private_with_random_name(self):
        self.submit()
        name = StudentDiscountApplication.objects.get().student_id_document.name
        self.assertNotIn("sam-smith", name)
        self.assertTrue((Path(self.private_dir) / name).exists())
        self.assertEqual(list(Path(self.public_dir).rglob("*.*")), [])
        self.assertEqual(self.client.get(f"/media/{name}").status_code, 404)

    def test_second_open_application_refused(self):
        self.submit()
        res = self.submit()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "ALREADY_APPLIED")

    def test_can_apply_again_after_rejection_or_expiry(self):
        self.submit()
        StudentDiscountApplication.objects.update(status="rejected")
        self.assertEqual(self.submit().status_code, 201)

        StudentDiscountApplication.objects.filter(status="pending").update(
            status="approved", graduation_date=timezone.localdate() - timedelta(days=1)
        )
        self.assertEqual(self.submit().status_code, 201)

    def test_validation_errors_keep_their_shape(self):
        res = self.submit(mobile="123")
        self.assertEqual(res.status_code, 400)
        self.assertIn("mobile", res.json())


class StatusTests(Base):

    def get_status(self):
        return self.client.get(STATUS_URL).json()["data"]

    def test_none_before_applying(self):
        self.assertEqual(self.get_status(), {"status": "none", "application": None})

    def test_under_review_shows_as_pending(self):
        self.submit()
        StudentDiscountApplication.objects.update(status="under_review")
        self.assertEqual(self.get_status()["status"], "pending")

    def test_approved_until_graduation_then_expired(self):
        self.submit()
        StudentDiscountApplication.objects.update(status="approved")
        data = self.get_status()
        self.assertEqual(data["status"], "approved")
        self.assertIsNotNone(data["application"]["valid_until"])

        StudentDiscountApplication.objects.update(
            graduation_date=timezone.localdate() - timedelta(days=1)
        )
        self.assertEqual(self.get_status()["status"], "expired")
        self.assertEqual(StudentDiscountApplication.objects.get().status, "expired")

    def test_rejected_shows_reason(self):
        self.submit()
        StudentDiscountApplication.objects.update(status="rejected", rejection_reason="ID unreadable")
        data = self.get_status()
        self.assertEqual(data["status"], "rejected")
        self.assertEqual(data["application"]["rejection_reason"], "ID unreadable")


class PlansTests(Base):

    def setUp(self):
        super().setUp()
        Plan.objects.create(
            name="Student 10GB", price=Decimal("10"), price_24=Decimal("5"),
            duration_days=30, audience=Plan.Audience.STUDENT,
        )
        Plan.objects.create(
            name="Public 10GB", price=Decimal("10"), duration_days=30,
            audience=Plan.Audience.PUBLIC_SECTOR,
        )

    def test_anyone_sees_student_plans_without_purchase(self):
        data = APIClient().get(PLANS_URL).json()["data"]
        self.assertFalse(data["can_purchase"])
        self.assertEqual([p["name"] for p in data["plans"]], ["Student 10GB"])

    def test_approved_can_purchase_until_expiry(self):
        self.submit()
        StudentDiscountApplication.objects.update(status="approved")
        self.assertTrue(self.client.get(PLANS_URL).json()["data"]["can_purchase"])

        StudentDiscountApplication.objects.update(
            graduation_date=timezone.localdate() - timedelta(days=1)
        )
        data = self.client.get(PLANS_URL).json()["data"]
        self.assertEqual(data["status"], "expired")
        self.assertFalse(data["can_purchase"])


class AdminDocumentTests(Base):

    def setUp(self):
        super().setUp()
        self.submit()
        self.application = StudentDiscountApplication.objects.get()
        self.url = f"/admin/student_discount/studentdiscountapplication/{self.application.pk}/document/"

    def test_staff_can_open_document(self):
        admin_user = User.objects.create_superuser("boss", "boss@example.com", "pw")
        self.client.force_login(admin_user)
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(b"".join(res.streaming_content), b"%PDF-1.4 test")
        self.assertEqual(res["Content-Type"], "application/pdf")

    def test_customer_cannot_open_document(self):
        self.client.force_login(self.user)
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 302)
        self.assertIn("/admin/login/", res["Location"])


class MoveDocumentsMigrationTests(Base):
    """0003 moves documents uploaded before this change out of public media."""

    def test_existing_public_document_is_moved_to_private_storage(self):
        migration = importlib.import_module(
            "apps.student_discount.migrations.0003_expire_link_and_move_documents"
        )
        public = FileSystemStorage(location=self.public_dir)
        private = FileSystemStorage(location=self.private_dir)
        old_name = public.save("student-discount/old-card.pdf", ContentFile(b"old file"))
        self.submit()
        StudentDiscountApplication.objects.update(student_id_document=old_name)

        with mock.patch("django.core.files.storage.default_storage", public), mock.patch(
            "apps.student_discount.storage.private_document_storage", return_value=private
        ):
            migration.move_documents(django_apps, None)

        self.assertFalse(public.exists(old_name))
        name = StudentDiscountApplication.objects.get().student_id_document.name
        with private.open(name) as moved:
            self.assertEqual(moved.read(), b"old file")
