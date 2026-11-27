from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings

from apps.sim_change.models import LineOperationLock
from apps.sim_change.services import crypto, line_lock

from .helpers import TEST_KEYS, make_line, make_user


@TEST_KEYS
class CryptoTests(TestCase):
    def test_fingerprint_ignores_formatting(self):
        self.assertEqual(crypto.fingerprint("+44 7700-900123"), crypto.fingerprint("447700900123"))
        self.assertNotEqual(crypto.fingerprint("447700900123"), crypto.fingerprint("447700900124"))
        self.assertEqual(crypto.fingerprint(""), "")

    def test_fingerprint_depends_on_key(self):
        first = crypto.fingerprint("8944000000000000001")
        with override_settings(SIM_CHANGE_FINGERPRINT_KEY="z" * 40):
            self.assertNotEqual(crypto.fingerprint("8944000000000000001"), first)

    def test_short_or_missing_key_fails_closed(self):
        for key in ("", "short"):
            with override_settings(SIM_CHANGE_FINGERPRINT_KEY=key):
                with self.assertRaises(crypto.CryptoUnavailable):
                    crypto.fingerprint("8944000000000000001")

    def test_same_identifier(self):
        stored = crypto.fingerprint("8944000000000000001")
        self.assertTrue(crypto.same_identifier(stored, "8944 0000 0000 0000 001"))
        self.assertFalse(crypto.same_identifier(stored, "8944000000000000002"))
        self.assertFalse(crypto.same_identifier("", "8944000000000000001"))

    def test_encrypt_round_trip_and_ciphertext_hides_value(self):
        token = crypto.encrypt_json({"postcode": "SW1A 1AA"})
        self.assertNotIn("SW1A", token)
        self.assertEqual(crypto.decrypt_json(token), {"postcode": "SW1A 1AA"})

    def test_encrypt_without_key_fails_closed(self):
        with override_settings(SIM_CHANGE_ENCRYPTION_KEY=""):
            with self.assertRaises(crypto.CryptoUnavailable):
                crypto.encrypt_json({"a": 1})

    def test_mask_serial(self):
        self.assertEqual(crypto.mask_serial("8944000000000001234"), "…1234")
        self.assertEqual(crypto.mask_serial(""), "")


class LineLockOutsideTransactionTests(TransactionTestCase):
    # TestCase wraps every test in a transaction, so this needs a real autocommit connection.
    def test_acquire_outside_transaction_is_refused(self):
        line = make_line(make_user())
        with self.assertRaises(RuntimeError):
            line_lock.acquire(line, "sim_type_change", "ref-1")


class LineLockTests(TestCase):
    def setUp(self):
        self.line = make_line(make_user())

    def test_acquire_is_idempotent_for_same_holder(self):
        with transaction.atomic():
            first = line_lock.acquire(self.line, "sim_type_change", "ref-1")
            again = line_lock.acquire(self.line, "sim_type_change", "ref-1")
        self.assertEqual(first.pk, again.pk)

    def test_other_operation_is_refused(self):
        with transaction.atomic():
            line_lock.acquire(self.line, "sim_type_change", "ref-1")
        with self.assertRaises(line_lock.LineBusy) as ctx, transaction.atomic():
            line_lock.acquire(self.line, "suspension", "ref-2")
        self.assertEqual(ctx.exception.code, "LINE_OPERATION_IN_PROGRESS")

    def test_release_then_reacquire(self):
        with transaction.atomic():
            line_lock.acquire(self.line, "sim_type_change", "ref-1")
        self.assertTrue(line_lock.release(self.line, "ref-1", reason="done"))
        self.assertFalse(line_lock.release(self.line, "ref-1"))
        with transaction.atomic():
            line_lock.acquire(self.line, "suspension", "ref-2")
        self.assertEqual(LineOperationLock.objects.filter(line=self.line).count(), 2)
