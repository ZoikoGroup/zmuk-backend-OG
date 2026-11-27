from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import UserProfile
from apps.security.models import AccountSecurityEvent
from apps.security.services import recent_changes

K = AccountSecurityEvent.Kind


class RecordingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "old-password-1")

    def kinds(self):
        return list(AccountSecurityEvent.objects.filter(user=self.user).values_list("kind", flat=True))

    def test_new_account_records_nothing(self):
        self.assertEqual(self.kinds(), [])

    def test_password_change_recorded(self):
        self.user.set_password("new-password-2")
        self.user.save()
        self.assertEqual(self.kinds(), [K.PASSWORD_CHANGED])

    def test_email_change_recorded_but_case_change_ignored(self):
        self.user.email = "ALICE@example.com "
        self.user.save()
        self.assertEqual(self.kinds(), [])
        self.user.email = "alice.new@example.com"
        self.user.save()
        self.assertEqual(self.kinds(), [K.EMAIL_CHANGED])

    def test_last_login_update_records_nothing(self):
        self.user.last_login = timezone.now()
        self.user.save(update_fields=["last_login"])
        self.user.first_name = "Alice"
        self.user.save()
        self.assertEqual(self.kinds(), [])

    def test_update_fields_limits_what_is_checked(self):
        self.user.email = "changed@example.com"
        self.user.save(update_fields=["first_name"])  # email not saved, so not a change
        self.assertEqual(self.kinds(), [])

    def test_profile_changes_recorded_per_field(self):
        profile = UserProfile.objects.get(user=self.user)
        profile.delivery_address = "1 New Street, London"
        profile.phone_number = "07700900123"
        profile.preferred_name = "Al"  # not watched
        profile.save()
        self.assertCountEqual(self.kinds(), [K.DELIVERY_ADDRESS_CHANGED, K.PHONE_CHANGED])

    def test_no_values_are_stored(self):
        profile = UserProfile.objects.get(user=self.user)
        profile.billing_address = "Secret Lane 9"
        profile.save()
        event = AccountSecurityEvent.objects.get(user=self.user)
        self.assertEqual(
            sorted(f.name for f in AccountSecurityEvent._meta.get_fields()),
            ["id", "kind", "occurred_at", "user"],
        )
        self.assertEqual(event.kind, K.BILLING_ADDRESS_CHANGED)


class RecentChangesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("bob", "bob@example.com", "pw-123456")

    @override_settings(ACCOUNT_SECURITY_EVENTS_SINCE="2026-01-01")
    def test_recent_change_found_with_complete_history(self):
        self.user.set_password("another-pass-3")
        self.user.save()
        result = recent_changes(self.user, days=14)
        self.assertTrue(result.changed(K.PASSWORD_CHANGED))
        self.assertTrue(result.history_complete)

    @override_settings(ACCOUNT_SECURITY_EVENTS_SINCE="2026-01-01")
    def test_old_change_outside_window_ignored(self):
        AccountSecurityEvent.objects.create(user=self.user, kind=K.EMAIL_CHANGED)
        AccountSecurityEvent.objects.filter(user=self.user).update(occurred_at=timezone.now() - timedelta(days=30))
        self.assertFalse(recent_changes(self.user, days=14).changed(K.EMAIL_CHANGED))

    def test_window_before_tracking_started_is_incomplete(self):
        today = timezone.localdate().isoformat()
        with override_settings(ACCOUNT_SECURITY_EVENTS_SINCE=today):
            result = recent_changes(self.user, days=14)
        self.assertEqual(result.kinds, set())
        self.assertFalse(result.history_complete)
        self.assertEqual(result.history_status, "UNKNOWN_PRE_TRACKING")
