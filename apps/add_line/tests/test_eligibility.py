from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.add_line.models import CapacityHold, Line, LineCapOverride
from apps.add_line.services import capacity, eligibility


@override_settings(MAX_CONSUMER_LINES=5, LINE_HOLD_MINUTES=30, ADD_LINE_REAUTH_MINUTES=60)
class CapacityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")

    def test_fresh_account_has_five_slots(self):
        usage = capacity.get_usage(self.user)
        self.assertEqual((usage.used, usage.limit, usage.remaining), (0, 5, 5))

    def test_live_lines_count_but_cancelled_and_terminated_do_not(self):
        for status in (Line.Status.PENDING_ACTIVATION, Line.Status.ACTIVE, Line.Status.SUSPENDED):
            Line.objects.create(owner=self.user, status=status)
        for status in (Line.Status.CANCELLED, Line.Status.TERMINATED):
            Line.objects.create(owner=self.user, status=status)
        self.assertEqual(capacity.get_usage(self.user).lines, 3)

    def test_sixth_slot_is_blocked(self):
        for _ in range(5):
            capacity.acquire_hold(self.user)
        with self.assertRaises(capacity.CapacityExceeded) as ctx:
            capacity.acquire_hold(self.user)
        self.assertEqual(ctx.exception.usage.used, 5)

    def test_lines_and_holds_share_the_cap(self):
        for _ in range(3):
            Line.objects.create(owner=self.user, status=Line.Status.ACTIVE)
        capacity.acquire_hold(self.user)
        capacity.acquire_hold(self.user)
        with self.assertRaises(capacity.CapacityExceeded):
            capacity.acquire_hold(self.user)

    def test_expired_hold_frees_its_slot(self):
        hold = capacity.acquire_hold(self.user)
        CapacityHold.objects.filter(pk=hold.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(capacity.get_usage(self.user).holds, 0)

    def test_paid_hold_never_expires(self):
        hold = capacity.acquire_hold(self.user)
        capacity.mark_paid(hold)
        hold.refresh_from_db()
        self.assertIsNone(hold.expires_at)
        self.assertEqual(capacity.get_usage(self.user).holds, 1)
        self.assertEqual(capacity.release_expired(), 0)

    def test_released_and_consumed_holds_do_not_count(self):
        capacity.release_hold(capacity.acquire_hold(self.user), "payment_failed")
        capacity.consume_hold(capacity.acquire_hold(self.user))
        self.assertEqual(capacity.get_usage(self.user).holds, 0)

    def test_release_does_not_revive_consumed_hold(self):
        hold = capacity.acquire_hold(self.user)
        capacity.consume_hold(hold)
        capacity.release_hold(hold, "late")
        hold.refresh_from_db()
        self.assertEqual(hold.status, CapacityHold.Status.CONSUMED)

    def test_support_override_raises_the_cap(self):
        LineCapOverride.objects.create(owner=self.user, max_lines=7, reason="large household")
        for _ in range(7):
            capacity.acquire_hold(self.user)
        with self.assertRaises(capacity.CapacityExceeded):
            capacity.acquire_hold(self.user)

    def test_caps_are_per_account(self):
        other = User.objects.create_user("bob", "bob@example.com", "pw")
        for _ in range(5):
            capacity.acquire_hold(self.user)
        capacity.acquire_hold(other)  # must not raise

    def test_release_expired_command(self):
        hold = capacity.acquire_hold(self.user)
        CapacityHold.objects.filter(pk=hold.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        out = StringIO()
        call_command("release_expired_line_holds", stdout=out)
        hold.refresh_from_db()
        self.assertEqual(hold.status, CapacityHold.Status.RELEASED)
        self.assertEqual(hold.release_reason, "expired")
        self.assertIn("released 1 expired", out.getvalue())


@override_settings(MAX_CONSUMER_LINES=5, ADD_LINE_REAUTH_MINUTES=60)
class EligibilityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.user.last_login = timezone.now()
        self.user.save()

    def test_verified_user_with_recent_login_is_eligible(self):
        result = eligibility.check(self.user)
        self.assertTrue(result.eligible)
        self.assertFalse(result.requires_reauth)

    def test_unverified_email_blocks(self):
        self.user.is_active = False
        result = eligibility.check(self.user)
        self.assertIn(eligibility.EMAIL_NOT_VERIFIED, result.reasons)

    def test_missing_email_blocks(self):
        self.user.email = ""
        self.assertIn(eligibility.EMAIL_NOT_VERIFIED, eligibility.check(self.user).reasons)

    def test_full_account_blocks(self):
        for _ in range(5):
            Line.objects.create(owner=self.user, status=Line.Status.ACTIVE)
        result = eligibility.check(self.user)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reasons, [eligibility.LINE_LIMIT_REACHED])

    def test_old_or_missing_login_requires_reauth_without_blocking(self):
        self.user.last_login = timezone.now() - timedelta(minutes=61)
        result = eligibility.check(self.user)
        self.assertTrue(result.eligible)
        self.assertTrue(result.requires_reauth)

        self.user.last_login = None
        self.assertTrue(eligibility.check(self.user).requires_reauth)


@override_settings(MAX_CONSUMER_LINES=5, ADD_LINE_REAUTH_MINUTES=60)
class EligibilityApiTests(TestCase):
    URL = "/api/v1/lines/eligibility/"

    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.client = APIClient()

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.URL).status_code, 401)

    def test_response_shape(self):
        Line.objects.create(owner=self.user, status=Line.Status.ACTIVE)
        capacity.acquire_hold(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.user).key}")

        resp = self.client.get(self.URL)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {
            "eligible": True,
            "reasons": [],
            "requires_reauth": True,  # never logged in
            "lines": {"active": 1, "in_progress": 1, "limit": 5, "remaining": 3},
        })

    def test_blocked_response_includes_message(self):
        for _ in range(5):
            Line.objects.create(owner=self.user, status=Line.Status.ACTIVE)
        self.client.force_authenticate(self.user)
        body = self.client.get(self.URL).json()
        self.assertFalse(body["eligible"])
        self.assertEqual(body["reasons"][0]["code"], "LINE_LIMIT_REACHED")
        self.assertTrue(body["reasons"][0]["message"])

    def test_login_records_last_login(self):
        """LoginAPI must set last_login, or the recent-login check can never pass."""
        self.assertIsNone(self.user.last_login)
        resp = self.client.post(
            "/api/accounts/login/", {"email": "alice@example.com", "password": "pw"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_login)
        self.assertFalse(eligibility.check(self.user).requires_reauth)
