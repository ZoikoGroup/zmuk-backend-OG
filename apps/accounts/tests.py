from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.accounts.models import UserProfile

UPDATE_PROFILE_URL = "/api/accounts/update-profile/"
DASHBOARD_URL = "/api/accounts/dashboard/"


class UpdateProfileTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="jane", email="jane@example.com", password="x-Strong-pass-1",
            first_name="Jane", last_name="Smith",
        )
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.user).key}")

    def _put(self, data):
        return self.client.put(UPDATE_PROFILE_URL, data, format="json")

    def _profile(self):
        return UserProfile.objects.get(user=self.user)

    # ---- happy paths ----

    def test_updates_all_new_fields(self):
        res = self._put({
            "preferred_name": "Jay",
            "phone_number": "07123456789",
            "billing_address": "1 High St, London, SW1A 1AA",
            "delivery_address": "2 Low Rd, Leeds, LS1 1AA",
            "preferences": {
                "contact_method": "sms",
                "accessibility": "Large text",
                "support_needs": "Prefers written contact",
            },
        })

        self.assertEqual(res.status_code, 200, res.data)
        profile = self._profile()
        self.assertEqual(profile.preferred_name, "Jay")
        self.assertEqual(profile.phone_number, "07123 456789")
        self.assertEqual(profile.billing_address, "1 High St, London, SW1A 1AA")
        self.assertEqual(profile.delivery_address, "2 Low Rd, Leeds, LS1 1AA")
        self.assertEqual(profile.preferences, {
            "contact_method": "sms",
            "accessibility": "Large text",
            "support_needs": "Prefers written contact",
        })
        # Response reflects what was saved.
        self.assertEqual(res.data["user"]["phone_number"], "07123 456789")
        self.assertEqual(res.data["user"]["preferences"]["contact_method"], "sms")

    def test_partial_update_keeps_other_fields(self):
        self._put({"preferred_name": "Jay", "billing_address": "1 High St, London"})
        res = self._put({"phone_number": "07123456789"})

        self.assertEqual(res.status_code, 200, res.data)
        profile = self._profile()
        self.assertEqual(profile.preferred_name, "Jay")
        self.assertEqual(profile.billing_address, "1 High St, London")
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Jane")

    def test_preferences_are_merged_not_replaced(self):
        self._put({"preferences": {"contact_method": "email", "support_needs": "Hearing impaired"}})
        res = self._put({"preferences": {"contact_method": "phone"}})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._profile().preferences, {
            "contact_method": "phone",
            "support_needs": "Hearing impaired",
        })

    def test_phone_formats_are_normalised(self):
        for raw in ["07123 456789", "+447123456789", "+44 7123-456-789", "(07123) 456789","00447123456789", "447123456789"]:
            res = self._put({"phone_number": raw})
            self.assertEqual(res.status_code, 200, (raw, res.data))
            self.assertEqual(self._profile().phone_number, "07123 456789", raw)

    def test_contact_method_accepts_frontend_labels(self):
        res = self._put({"preferences": {"contact_method": "Email"}})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._profile().preferences["contact_method"], "email")

    def test_blank_values_clear_fields(self):
        self._put({"phone_number": "07123456789", "delivery_address": "2 Low Rd, Leeds"})
        res = self._put({"phone_number": "", "delivery_address": ""})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._profile().phone_number, "")
        self.assertEqual(self._profile().delivery_address, "")

    def test_existing_fields_still_work(self):
        res = self._put({"first_name": "Janet", "bq_enrollment_id": "BQ-1"})

        self.assertEqual(res.status_code, 200, res.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Janet")
        self.assertEqual(self._profile().bq_enrollment_id, "BQ-1")

    # ---- validation ----

    def test_rejects_invalid_phone(self):
        for raw in ["0207 946 0000", "12345", "07123", "not a phone"]:
            res = self._put({"phone_number": raw})
            self.assertEqual(res.status_code, 400, raw)
            self.assertIn("phone_number", res.data)
        self.assertEqual(self._profile().phone_number, "")

    def test_rejects_invalid_preferred_name(self):
        for name in ["J4y", "<script>", "a" * 51]:
            res = self._put({"preferred_name": name})
            self.assertEqual(res.status_code, 400, name)
            self.assertIn("preferred_name", res.data)

    def test_accepts_names_with_hyphens_and_apostrophes(self):
        res = self._put({"preferred_name": "Mary-Jane O'Neil"})
        self.assertEqual(res.status_code, 200, res.data)

    def test_rejects_short_and_long_addresses(self):
        self.assertEqual(self._put({"billing_address": "abc"}).status_code, 400)
        self.assertEqual(self._put({"delivery_address": "x" * 301}).status_code, 400)

    def test_rejects_unknown_preference_keys(self):
        res = self._put({"preferences": {"contact_method": "email", "is_admin": True}})

        self.assertEqual(res.status_code, 400)
        self.assertIn("is_admin", res.data["preferences"])
        self.assertEqual(self._profile().preferences, {})

    def test_rejects_invalid_preference_values(self):
        self.assertEqual(self._put({"preferences": {"contact_method": "pigeon"}}).status_code, 400)
        self.assertEqual(self._put({"preferences": {"support_needs": "x" * 501}}).status_code, 400)
        self.assertEqual(self._put({"preferences": "email"}).status_code, 400)
        self.assertEqual(self._put({"preferences": None}).status_code, 400)

    def test_invalid_request_saves_nothing(self):
        res = self._put({"preferred_name": "Jay", "phone_number": "bad"})

        self.assertEqual(res.status_code, 400)
        self.assertEqual(self._profile().preferred_name, "")

    # ---- auth & isolation ----

    def test_requires_login(self):
        anon = APIClient()
        self.assertEqual(anon.get(UPDATE_PROFILE_URL).status_code, 401)
        self.assertEqual(anon.put(UPDATE_PROFILE_URL, {"preferred_name": "X"}, format="json").status_code, 401)

    def test_only_updates_own_profile(self):
        other = User.objects.create_user(username="bob", email="bob@example.com", password="x-Strong-pass-1")
        self._put({"preferred_name": "Jay", "user": other.pk, "id": other.pk})

        self.assertEqual(UserProfile.objects.get(user=other).preferred_name, "")
        self.assertEqual(self._profile().preferred_name, "Jay")

    # ---- reading ----

    def test_get_returns_full_profile_with_default_preferences(self):
        res = self.client.get(UPDATE_PROFILE_URL)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["email"], "jane@example.com")
        self.assertEqual(res.data["preferred_name"], "")
        self.assertEqual(res.data["preferences"], {
            "contact_method": "", "accessibility": "", "support_needs": "",
        })

    def test_dashboard_includes_profile_fields(self):
        self._put({"preferred_name": "Jay", "phone_number": "07123456789",
                   "preferences": {"contact_method": "sms"}})
        res = self.client.get(DASHBOARD_URL)

        self.assertEqual(res.status_code, 200)
        self.assertNotIn("password", res.data)
        self.assertEqual(res.data["preferred_name"], "Jay")
        self.assertEqual(res.data["phone_number"], "07123 456789")
        self.assertEqual(res.data["preferences"]["contact_method"], "sms")
        self.assertEqual(res.data["preferences"]["support_needs"], "")

    def test_user_without_profile_row_does_not_error(self):
        UserProfile.objects.filter(user=self.user).delete()

        self.assertEqual(self.client.get(UPDATE_PROFILE_URL).status_code, 200)
        self.assertEqual(self.client.get(DASHBOARD_URL).status_code, 200)
        self.assertEqual(self._put({"preferred_name": "Jay"}).status_code, 200)
        self.assertEqual(self._profile().preferred_name, "Jay")
