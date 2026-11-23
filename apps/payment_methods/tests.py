"""Saved payment methods. Test names reference the decision note's QA set (§08)."""

import json
from datetime import timedelta
from decimal import Decimal
from secrets import token_hex
from types import SimpleNamespace
from unittest import mock

import stripe
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.add_line.services import payments as add_line_payments
from apps.add_line.tests.test_checkout import CheckoutTestCase as AddLineCheckoutBase
from apps.add_line.tests.test_checkout import fake_intent as add_line_intent
from apps.add_line.tests.test_checkout import fake_service
from apps.orders.models import CheckoutPayment
from apps.orders.tests import body as checkout_body
from apps.orders.tests import make_plan, plan_line

from apps.payment_methods import services
from apps.payment_methods.models import PaymentMethod, PaymentMethodConsent, PaymentMethodSetup, StripeCustomer

User = get_user_model()

BASE = "/api/v1/payment-methods/"
CONSENT = services.SAVE_CARD_CONSENT_VERSION
SETTINGS = dict(
    STRIPE_SECRET_KEY="sk_test_x",
    SAVED_PAYMENT_METHODS_ENABLED=True,
    SAVED_PAYMENT_METHODS_MAX=3,
    SAVED_PAYMENT_METHODS_REAUTH_MINUTES=15,
    # Random per run: construct_event is mocked, the value only has to pass usable_secret().
    STRIPE_PAYMENT_METHODS_WEBHOOK_SECRET=f"whsec_{token_hex(16)}",
)


def stripe_pm(pm_id="pm_1", customer="cus_1", fingerprint="fp_1", last4="4242", exp_year=2030, pm_type="card"):
    return {
        "id": pm_id, "type": pm_type, "customer": customer,
        "card": {"brand": "visa", "last4": last4, "exp_month": 8, "exp_year": exp_year, "fingerprint": fingerprint},
    }


def setup_intent(si_id="seti_1", customer="cus_1", pm=None, status="succeeded"):
    return {
        "id": si_id, "object": "setup_intent", "customer": customer, "status": status, "livemode": False,
        "payment_method": pm if pm is not None else stripe_pm(customer=customer),
        "metadata": {"source": services.STRIPE_SOURCE},
    }


class StripeMocks:
    """Patches every Stripe call the app makes; nothing reaches Stripe."""

    def start_stripe_mocks(self):
        self.customer_create = self._patch(
            "apps.payment_methods.services.stripe.Customer.create",
            side_effect=lambda **kw: SimpleNamespace(id=f"cus_{kw['metadata']['user_id']}"),
        )
        self.si_counter = 0

        def make_si(**kwargs):
            self.si_counter += 1
            return SimpleNamespace(id=f"seti_{self.si_counter}", client_secret=f"seti_{self.si_counter}_secret")

        self.si_create = self._patch("apps.payment_methods.services.stripe.SetupIntent.create", side_effect=make_si)
        self.si_retrieve = self._patch("apps.payment_methods.services.stripe.SetupIntent.retrieve")
        self.pm_retrieve = self._patch("apps.payment_methods.services.stripe.PaymentMethod.retrieve")
        self.pm_detach = self._patch("apps.payment_methods.services.stripe.PaymentMethod.detach")

    def _patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()


@override_settings(**SETTINGS)
class Base(StripeMocks, TestCase):
    def setUp(self):
        cache.clear()  # throttles
        self.start_stripe_mocks()
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.other = User.objects.create_user("bob", "bob@example.com", "pw")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def add_card(self, user=None, n=1, fingerprint=None):
        """Save a card through the real dashboard flow."""
        user = user or self.user
        client = APIClient()
        client.force_authenticate(user)
        res = client.post(f"{BASE}setup-intent/", {"consent": True, "consent_version": CONSENT}, format="json")
        assert res.status_code == 201, res.content
        setup = PaymentMethodSetup.objects.filter(user=user).first()
        customer = setup.customer.stripe_customer_id
        self.si_retrieve.return_value = setup_intent(
            setup.stripe_intent_id, customer,
            stripe_pm(f"pm_{user.pk}_{n}", customer, fingerprint or f"fp_{user.pk}_{n}", last4=f"{n:04d}"),
        )
        return client.post(f"{BASE}setup-intent/confirm/", {"setup_intent_id": setup.stripe_intent_id}, format="json")


class ListTests(Base):
    def test_disabled_feature_says_so(self):
        with override_settings(SAVED_PAYMENT_METHODS_ENABLED=False):
            res = self.client.get(BASE)
            self.assertEqual(res.json(), {"enabled": False, "limit": 0, "can_add": False, "payment_methods": []})
            setup = self.client.post(f"{BASE}setup-intent/", {"consent": True, "consent_version": CONSENT}, format="json")
            self.assertEqual(setup.status_code, 404)

    def test_login_required(self):
        self.assertEqual(APIClient().get(BASE).status_code, 401)

    def test_lists_own_cards_without_stripe_ids_or_fingerprint(self):
        self.add_card()
        self.add_card(user=self.other)
        data = self.client.get(BASE).json()
        self.assertTrue(data["enabled"])
        self.assertEqual((data["limit"], data["can_add"], data["password_required"]), (3, True, True))
        self.assertEqual(data["consent"]["version"], CONSENT)
        self.assertEqual(len(data["payment_methods"]), 1)
        card = data["payment_methods"][0]
        self.assertEqual((card["brand"], card["last4"], card["is_default"]), ("visa", "0001", True))
        raw = json.dumps(data)
        self.assertNotIn("fp_", raw)
        self.assertNotIn("pm_", raw)
        self.assertNotIn("cus_", raw)


class AddCardTests(Base):
    def test_consent_must_be_ticked_and_current(self):
        res = self.client.post(f"{BASE}setup-intent/", {"consent": False, "consent_version": CONSENT}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.post(f"{BASE}setup-intent/", {"consent": True, "consent_version": "old"}, format="json")
        self.assertEqual(res.json()["code"], "CONSENT_OUTDATED")
        self.si_create.assert_not_called()

    def test_setup_intent_saves_without_charge_for_customer_present_use(self):
        # QA 01: a SetupIntent, never a PaymentIntent; card entry is Stripe's.
        res = self.client.post(f"{BASE}setup-intent/", {"consent": True, "consent_version": CONSENT}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["client_secret"], "seti_1_secret")
        kwargs = self.si_create.call_args.kwargs
        self.assertEqual(kwargs["usage"], "on_session")
        self.assertEqual(kwargs["payment_method_types"], ["card"])
        self.assertEqual(kwargs["customer"], f"cus_{self.user.pk}")
        consent = PaymentMethodConsent.objects.get()
        self.assertEqual((consent.kind, consent.wording_text), ("save_for_my_payments", services.SAVE_CARD_CONSENT_TEXT))

    def test_one_stripe_customer_per_user(self):
        self.add_card(n=1)
        self.add_card(n=2)
        self.assertEqual(self.customer_create.call_count, 1)
        self.assertEqual(StripeCustomer.objects.filter(user=self.user).count(), 1)

    def test_confirm_saves_card_and_first_is_default(self):
        res = self.add_card()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["message"], "Card added. No payment has been taken.")
        method = PaymentMethod.objects.get()
        self.assertTrue(method.is_default)
        self.assertEqual(method.allowed_usage, "on_session")
        self.assertEqual(PaymentMethodConsent.objects.get().payment_method, method)

        self.add_card(n=2)
        self.assertEqual(PaymentMethod.objects.filter(is_default=True).count(), 1)

    def test_confirm_twice_and_webhook_do_not_duplicate(self):
        # QA 02
        self.add_card()
        setup = PaymentMethodSetup.objects.get()
        again = self.client.post(f"{BASE}setup-intent/confirm/", {"setup_intent_id": setup.stripe_intent_id}, format="json")
        self.assertEqual(again.status_code, 200)
        services.finish_setup(self.si_retrieve.return_value)
        self.assertEqual(PaymentMethod.objects.count(), 1)

    def test_same_card_twice_is_refused_and_detached(self):
        self.add_card(n=1, fingerprint="same")
        res = self.add_card(n=2, fingerprint="same")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "DUPLICATE_CARD")
        self.pm_detach.assert_called_once_with(f"pm_{self.user.pk}_2")
        self.assertEqual(PaymentMethod.objects.count(), 1)

    def test_limit_of_three(self):
        # QA 04
        for n in (1, 2, 3):
            self.add_card(n=n)
        res = self.client.post(f"{BASE}setup-intent/", {"consent": True, "consent_version": CONSENT}, format="json")
        self.assertEqual((res.status_code, res.json()["code"]), (409, "LIMIT_REACHED"))
        self.assertFalse(self.client.get(BASE).json()["can_add"])

    def test_limit_rechecked_when_saving(self):
        for n in (1, 2):
            self.add_card(n=n)
        # Two setups started while there was still room...
        for _ in range(2):
            self.client.post(f"{BASE}setup-intent/", {"consent": True, "consent_version": CONSENT}, format="json")
        first, second = PaymentMethodSetup.objects.filter(status="pending").order_by("created_at")
        cus = first.customer.stripe_customer_id
        services.finish_setup(setup_intent(first.stripe_intent_id, cus, stripe_pm("pm_a", cus, "fp_a")))
        services.finish_setup(setup_intent(second.stripe_intent_id, cus, stripe_pm("pm_b", cus, "fp_b")))
        second.refresh_from_db()
        self.assertEqual(second.status, "limit_reached")
        self.pm_detach.assert_called_with("pm_b")
        self.assertEqual(services.active_methods(self.user).count(), 3)

    def test_not_completed_setup_saves_nothing(self):
        self.client.post(f"{BASE}setup-intent/", {"consent": True, "consent_version": CONSENT}, format="json")
        setup = PaymentMethodSetup.objects.get()
        self.si_retrieve.return_value = setup_intent(setup.stripe_intent_id, setup.customer.stripe_customer_id,
                                                     status="requires_action")
        res = self.client.post(f"{BASE}setup-intent/confirm/", {"setup_intent_id": setup.stripe_intent_id}, format="json")
        self.assertEqual(res.status_code, 402)
        self.assertFalse(PaymentMethod.objects.exists())

    def test_cannot_confirm_another_accounts_setup(self):
        # QA 03
        self.add_card(user=self.other)
        setup = PaymentMethodSetup.objects.get(user=self.other)
        res = self.client.post(f"{BASE}setup-intent/confirm/", {"setup_intent_id": setup.stripe_intent_id}, format="json")
        self.assertEqual(res.status_code, 404)

    def test_card_from_another_customer_is_refused(self):
        self.client.post(f"{BASE}setup-intent/", {"consent": True, "consent_version": CONSENT}, format="json")
        setup = PaymentMethodSetup.objects.get()
        self.si_retrieve.return_value = setup_intent(setup.stripe_intent_id, "cus_someone_else")
        res = self.client.post(f"{BASE}setup-intent/confirm/", {"setup_intent_id": setup.stripe_intent_id}, format="json")
        self.assertEqual((res.status_code, res.json()["code"]), (409, "SETUP_MISMATCH"))
        self.assertFalse(PaymentMethod.objects.exists())


class ManageTests(Base):
    def setUp(self):
        super().setUp()
        for n in (1, 2, 3):
            self.add_card(n=n)
        self.first, self.second, self.third = PaymentMethod.objects.order_by("created_at")

    def test_set_default_needs_password(self):
        url = f"{BASE}{self.second.pk}/default/"
        res = self.client.post(url, {"password": "wrong"}, format="json")
        self.assertEqual((res.status_code, res.json()["code"]), (403, "PASSWORD_INCORRECT"))
        res = self.client.post(url, {"password": "pw"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(list(PaymentMethod.objects.filter(is_default=True)), [self.second])

    def test_remove_default_promotes_newest_card(self):
        res = self.client.post(f"{BASE}{self.first.pk}/remove/", {"password": "pw"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.pm_detach.assert_called_once_with(self.first.stripe_payment_method_id)
        self.first.refresh_from_db()
        self.assertEqual((self.first.status, self.first.is_default), ("removed", False))
        self.assertEqual(res.json()["new_default"]["id"], str(self.third.pk))

    def test_remove_when_stripe_already_detached(self):
        self.pm_detach.side_effect = stripe.error.InvalidRequestError("No such attachment", "payment_method")
        res = self.client.post(f"{BASE}{self.second.pk}/remove/", {"password": "pw"}, format="json")
        self.assertEqual(res.status_code, 200)

    def test_cannot_touch_another_accounts_card(self):
        # QA 03
        client = APIClient()
        client.force_authenticate(self.other)
        for action in ("default", "remove"):
            res = client.post(f"{BASE}{self.first.pk}/{action}/", {"password": "pw"}, format="json")
            self.assertEqual(res.status_code, 404)
        self.pm_detach.assert_not_called()

    def test_social_login_needs_recent_login(self):
        social = User.objects.create_user("sam", "sam@example.com")
        social.set_unusable_password()
        social.last_login = timezone.now() - timedelta(hours=2)
        social.save()
        self.add_card(user=social, n=9)
        method = PaymentMethod.objects.get(user=social)
        client = APIClient()
        client.force_authenticate(social)
        res = client.post(f"{BASE}{method.pk}/remove/", {}, format="json")
        self.assertEqual(res.json()["code"], "REAUTH_REQUIRED")
        social.last_login = timezone.now()
        social.save()
        self.assertEqual(client.post(f"{BASE}{method.pk}/remove/", {}, format="json").status_code, 200)


@override_settings(**SETTINGS)
class WebhookTests(Base):
    def send(self, event_type, obj):
        event = {"id": "evt_1", "type": event_type, "livemode": False, "data": {"object": obj}}
        with mock.patch("apps.payment_methods.views.stripe.Webhook.construct_event", return_value=event):
            return APIClient().post(f"{BASE}webhook/", data=json.dumps(event), content_type="application/json",
                                    HTTP_STRIPE_SIGNATURE="t=1,v1=x")

    def test_setup_succeeded_saves_card_if_browser_never_confirmed(self):
        self.client.post(f"{BASE}setup-intent/", {"consent": True, "consent_version": CONSENT}, format="json")
        setup = PaymentMethodSetup.objects.get()
        res = self.send("setup_intent.succeeded", setup_intent(setup.stripe_intent_id, setup.customer.stripe_customer_id))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(PaymentMethod.objects.count(), 1)
        # Replay (QA 09): still one card.
        self.send("setup_intent.succeeded", setup_intent(setup.stripe_intent_id, setup.customer.stripe_customer_id))
        self.assertEqual(PaymentMethod.objects.count(), 1)

    def test_other_setup_intents_ignored(self):
        res = self.send("setup_intent.succeeded", {**setup_intent("seti_x"), "metadata": {}})
        self.assertEqual(res.json()["ignored"], "not a saved-card setup")

    def test_unknown_setup_is_acknowledged(self):
        res = self.send("setup_intent.succeeded", setup_intent("seti_unknown"))
        self.assertEqual((res.status_code, res.json()["ignored"]), (200, "SETUP_NOT_FOUND"))

    def test_detached_in_stripe_removes_card(self):
        self.add_card()
        method = PaymentMethod.objects.get()
        self.send("payment_method.detached", stripe_pm(method.stripe_payment_method_id, None))
        method.refresh_from_db()
        self.assertEqual(method.status, "removed")

    def test_reissued_card_updates_expiry(self):
        self.add_card()
        method = PaymentMethod.objects.get()
        self.send("payment_method.automatically_updated",
                  stripe_pm(method.stripe_payment_method_id, last4="9999", exp_year=2033))
        method.refresh_from_db()
        self.assertEqual((method.last4, method.exp_year), ("9999", 2033))

    def test_bad_signature_rejected(self):
        with mock.patch("apps.payment_methods.views.stripe.Webhook.construct_event",
                        side_effect=stripe.error.SignatureVerificationError("bad", "sig")):
            res = APIClient().post(f"{BASE}webhook/", data="{}", content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_missing_secret_fails_closed(self):
        with override_settings(STRIPE_PAYMENT_METHODS_WEBHOOK_SECRET=""):
            res = APIClient().post(f"{BASE}webhook/", data="{}", content_type="application/json")
        self.assertEqual(res.status_code, 503)


CHECKOUT_URL = "/api/v1/checkout/create-intent/"


@override_settings(**SETTINGS)
class MainCheckoutTests(Base):
    def setUp(self):
        super().setUp()
        self.intent_create = self._patch(
            "apps.orders.views.stripe.PaymentIntent.create",
            return_value=SimpleNamespace(id="pi_123", client_secret="pi_123_secret"),
        )
        self.plan = make_plan()

    def order(self, client=None, **extra):
        return (client or self.client).post(
            CHECKOUT_URL, checkout_body([plan_line(self.plan, 730, "8.00")], 8, **extra), format="json",
        )

    def test_pay_with_saved_card_sets_card_on_intent(self):
        self.add_card()
        method = PaymentMethod.objects.get()
        res = self.order(paymentMethodId=str(method.pk))
        self.assertEqual(res.status_code, 200, res.content)
        kwargs = self.intent_create.call_args.kwargs
        self.assertEqual(kwargs["customer"], method.customer.stripe_customer_id)
        self.assertEqual(kwargs["payment_method"], method.stripe_payment_method_id)
        self.assertNotIn("automatic_payment_methods", kwargs)
        # QA 07: the customer confirms in the browser; never charged off-session.
        self.assertNotIn("off_session", kwargs)
        self.assertNotIn("confirm", kwargs)
        payment = CheckoutPayment.objects.get()
        self.assertEqual(payment.saved_payment_method, method)
        self.assertNotIn("paymentMethodId", payment.payload)

    def test_another_accounts_card_is_refused(self):
        # QA 03
        self.add_card(user=self.other)
        res = self.order(paymentMethodId=str(PaymentMethod.objects.get().pk))
        self.assertEqual((res.status_code, res.json()["code"]), (404, "PAYMENT_METHOD_NOT_FOUND"))
        self.assertFalse(CheckoutPayment.objects.exists())
        self.intent_create.assert_not_called()

    def test_guest_cannot_use_saved_cards(self):
        res = self.order(client=APIClient(), paymentMethodId="0f6c3c2e-0000-0000-0000-000000000000")
        self.assertEqual((res.status_code, res.json()["code"]), (401, "LOGIN_REQUIRED"))

    def test_disabled_feature_refuses_saved_card_requests(self):
        with override_settings(SAVED_PAYMENT_METHODS_ENABLED=False):
            res = self.order(saveCard=True, saveCardConsentVersion=CONSENT)
        self.assertEqual(res.json()["code"], "SAVED_CARDS_UNAVAILABLE")

    def test_plain_checkout_unchanged(self):
        self.assertEqual(self.order().status_code, 200)
        self.assertEqual(self.intent_create.call_args.kwargs["automatic_payment_methods"], {"enabled": True})
        self.assertNotIn("customer", self.intent_create.call_args.kwargs)

    def test_save_card_at_checkout_then_webhook_saves_it(self):
        res = self.order(saveCard=True, saveCardConsentVersion=CONSENT)
        self.assertEqual(res.status_code, 200, res.content)
        kwargs = self.intent_create.call_args.kwargs
        self.assertEqual(kwargs["setup_future_usage"], "on_session")
        self.assertEqual(kwargs["payment_method_types"], ["card"])
        setup = PaymentMethodSetup.objects.get()
        self.assertEqual((setup.source, setup.stripe_intent_id), ("checkout", "pi_123"))

        payment = CheckoutPayment.objects.get()
        cus = setup.customer.stripe_customer_id
        self.pm_retrieve.return_value = stripe_pm("pm_paid", cus, "fp_paid")
        intent = {"id": "pi_123", "status": "succeeded", "customer": cus, "livemode": False,
                  "payment_method": "pm_paid", "metadata": {"order_ref": payment.order_ref}}
        event = {"type": "payment_intent.succeeded", "data": {"object": intent}}
        with mock.patch("apps.orders.views.stripe.Webhook.construct_event", return_value=event), \
                mock.patch("apps.orders.views._process_checkout_payment"), \
                override_settings(STRIPE_CHECKOUT_WEBHOOK_SECRET="whsec_x"):
            APIClient().post("/api/v1/checkout/webhook/", data="{}", content_type="application/json")
        method = PaymentMethod.objects.get()
        self.assertEqual((method.stripe_payment_method_id, method.is_default), ("pm_paid", True))

    def test_save_card_and_saved_card_together_refused(self):
        res = self.order(paymentMethodId="0f6c3c2e-0000-0000-0000-000000000000", saveCard=True,
                         saveCardConsentVersion=CONSENT)
        self.assertEqual(res.json()["code"], "INVALID_REQUEST")


@override_settings(**SETTINGS)
class AddLineCheckoutTests(StripeMocks, AddLineCheckoutBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.start_stripe_mocks()

    def saved_card(self):
        customer = StripeCustomer.objects.create(user=self.user, stripe_customer_id="cus_al", livemode=False)
        return PaymentMethod.objects.create(
            user=self.user, customer=customer, stripe_payment_method_id="pm_al", brand="visa", last4="4242",
            fingerprint="fp_al", is_default=True,
        )

    def test_pay_with_saved_card(self):
        method = self.saved_card()
        resp, order = self.checkout(payment_method_id=str(method.pk))
        self.assertEqual(resp.status_code, 201, resp.content)
        kwargs = self.create_intent.call_args.kwargs
        self.assertEqual((kwargs["customer"], kwargs["payment_method"]), ("cus_al", "pm_al"))
        self.assertNotIn("automatic_payment_methods", kwargs)
        self.assertEqual(order.saved_payment_method, method)

    def test_unknown_card_refused_before_holding_anything(self):
        resp, order = self.checkout(payment_method_id="0f6c3c2e-0000-0000-0000-000000000000")
        self.assertEqual(resp.json()["code"], "PAYMENT_METHOD_NOT_FOUND")
        self.assertIsNone(order)
        self.create_intent.assert_not_called()

    def test_save_card_when_paid(self):
        resp, order = self.checkout(save_card=True, save_card_consent_version=CONSENT)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(self.create_intent.call_args.kwargs["setup_future_usage"], "on_session")
        cus = StripeCustomer.objects.get(user=self.user).stripe_customer_id
        self.pm_retrieve.return_value = stripe_pm("pm_new", cus, "fp_new")
        intent = add_line_intent(order, customer=cus, payment_method="pm_new")
        with mock.patch("apps.add_line.services.fulfilment.get_service", return_value=fake_service()):
            add_line_payments.handle_payment_succeeded(order, intent)
        self.assertEqual(PaymentMethod.objects.get(user=self.user).stripe_payment_method_id, "pm_new")
        self.assertEqual(PaymentMethodSetup.objects.get().source, "add_line")
