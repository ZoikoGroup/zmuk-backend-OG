"""Structured UK delivery address (CTO v2.0 §11.1, Addendum v2.1 B5)."""

from django.test import TestCase

from apps.accounts.models import UserProfile
from apps.sim_change.services import address
from apps.sim_change.services.address import AddressError
from apps.sim_change.services.transitions import TransitionError, open_case, set_fulfilment

from .helpers import SUP, TEST_KEYS, Staff, make_line, make_user, to_stock_reserved

GOOD = {"line1": " 10  Downing Street ", "town": "London", "postcode": "sw1a2aa"}


class NormaliseTests(TestCase):
    def test_postcode_normalised(self):
        for raw, expected in (("sw1a2aa", "SW1A 2AA"), ("M1 1AE", "M1 1AE"), ("b33 8th", "B33 8TH"),
                              ("CR2 6XH", "CR2 6XH"), ("dn55 1pt", "DN55 1PT"), ("gir0aa", "GIR 0AA")):
            self.assertEqual(address.normalise_postcode(raw), expected, raw)
        for bad in ("", "12345", "SW1A", "QQ1 1AA", "SW1A 2AAA"):
            self.assertIsNone(address.normalise_postcode(bad), bad)

    def test_clean_address(self):
        clean = address.normalise_uk_address(GOOD)
        self.assertEqual(clean, {"line1": "10 Downing Street", "line2": "", "town": "London", "county": "",
                                 "postcode": "SW1A 2AA", "country": "GB"})

    def test_field_errors(self):
        with self.assertRaises(AddressError) as ctx:
            address.normalise_uk_address({"line1": "", "town": "x" * 61, "postcode": "nope", "country": "FR"})
        self.assertEqual(set(ctx.exception.errors), {"line1", "town", "postcode", "country"})

    def test_control_characters_rejected(self):
        with self.assertRaises(AddressError) as ctx:
            address.normalise_uk_address({**GOOD, "line2": "Flat 1\x00"})
        self.assertIn("line2", ctx.exception.errors)


@TEST_KEYS
class SetDeliveryAddressTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.staff = Staff()
        self.case = open_case(owner=self.user, line=make_line(self.user, esim=True), to_type="psim")

    def test_stored_encrypted_and_events_hold_no_address(self):
        case = address.set_delivery_address(self.case, GOOD, source="on_file")
        self.assertNotIn("Downing", case.delivery_snapshot_encrypted)
        self.assertEqual(address.delivery_address(case)["postcode"], "SW1A 2AA")
        self.assertEqual(case.delivery_address_source, "on_file")
        self.assertNotIn("Downing", str(list(case.events.values_list("detail", flat=True))))

    def test_new_address_flagged_for_review(self):
        case = address.set_delivery_address(self.case, GOOD, source="new")
        self.assertIn(address.REVIEW_FLAG, case.risk_flags)

    def test_recent_account_address_change_flagged(self):
        profile = UserProfile.objects.get(user=self.user)
        profile.delivery_address = "somewhere new"
        profile.save()
        case = address.set_delivery_address(self.case, GOOD, source="on_file")
        self.assertIn(address.REVIEW_FLAG, case.risk_flags)

    def test_on_file_without_recent_change_not_flagged_and_flag_cleared(self):
        case = address.set_delivery_address(self.case, GOOD, source="new")
        case = address.set_delivery_address(case, GOOD, source="on_file")
        self.assertNotIn(address.REVIEW_FLAG, case.risk_flags)

    def test_esim_case_rejected(self):
        esim_case = open_case(owner=self.user, line=make_line(self.user), to_type="esim")
        with self.assertRaises(TransitionError) as ctx:
            address.set_delivery_address(esim_case, GOOD, source="on_file")
        self.assertEqual(ctx.exception.code, "NOT_PHYSICAL")

    def test_address_locked_once_shipped(self):
        case = to_stock_reserved(self.case, self.staff)
        case = set_fulfilment(case, "physical", "AWAITING_DISPATCH", source=SUP)
        address.set_delivery_address(case, GOOD, source="on_file")  # still allowed before shipping
        case = set_fulfilment(case, "physical", "SHIPPED", source=SUP, carrier="DPD", tracking_reference="1")
        with self.assertRaises(TransitionError) as ctx:
            address.set_delivery_address(case, GOOD, source="on_file")
        self.assertEqual(ctx.exception.code, "ADDRESS_LOCKED")

    def test_same_address_same_fingerprint(self):
        a = address.address_fingerprint(address.normalise_uk_address(GOOD))
        b = address.address_fingerprint(address.normalise_uk_address({**GOOD, "postcode": "SW1A 2AA"}))
        self.assertEqual(a, b)
