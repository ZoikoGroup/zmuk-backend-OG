from datetime import timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.add_line.models import AddLineOrder, CapacityHold, ContractAcceptance, Line
from apps.add_line.services import payments
from apps.add_line.services.quote import build_quote, issue_token
from apps.coupons.models import Coupon, CouponUsage
from apps.plans.models import Plan
from apps.sims.models import Order as SimOrder
from apps.sims.models import Sim, SimReservation
from apps.sims.transatel import TransatelAPIError

CHECKOUT_URL = "/api/v1/lines/checkout/"
SETTINGS = dict(
    STRIPE_SECRET_KEY="sk_test_x",
    STRIPE_ADD_LINE_WEBHOOK_SECRET="whsec_x",
    MAX_CONSUMER_LINES=5,
    LINE_HOLD_MINUTES=30,
    ADD_LINE_REAUTH_MINUTES=60,
    ADD_LINE_QUOTE_MINUTES=30,
    ADD_LINE_CONTRACT_VERSION="v1",
    ADD_LINE_CONTRACT_SUMMARY_VERSION="s1",
)


def make_sim(n=1, sim_type="eUICC"):
    return Sim.objects.create(
        iccid=f"894400000000000000{n}", msisdn=f"44770090000{n}",
        provisioning_status="Available", type_of_sim=sim_type,
    )


def fake_intent(order, status="succeeded", **overrides):
    return {
        "id": order.stripe_payment_intent_id,
        "status": status,
        "amount": int(order.total_gross * 100),
        "amount_received": int(order.total_gross * 100),
        "currency": "gbp",
        "metadata": {"add_line_order": order.order_reference, "source": "zoiko_add_line"},
        **overrides,
    }


def fake_service(post_status="Active", activate_error=None, subscriber_status="Active"):
    service = mock.Mock()
    if activate_error:
        service.activate_order_sim.side_effect = activate_error
    else:
        service.activate_order_sim.return_value = {
            "msisdn": "447700900001", "post_status": post_status,
            "transaction_id": "tx-1", "contact": {"status": "success"},
        }
    service.get_subscriber_by_serial.return_value = {"status": subscriber_status}
    return service


@override_settings(**SETTINGS)
class CheckoutTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.user.last_login = timezone.now()
        self.user.save()
        self.plan = Plan.objects.create(
            name="Standard 10GB", price=Decimal("10.00"), price_30=Decimal("10.00"),
            duration_days=30, transatelID="TSL_UK_10GB",
        )
        self.sim = make_sim()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        patcher = mock.patch("apps.add_line.services.checkout.stripe.PaymentIntent.create",
                             side_effect=self._create_intent)
        self.create_intent = patcher.start()
        self.addCleanup(patcher.stop)
        for target in ("deliver_qr", "deliver_details"):
            p = mock.patch(f"apps.add_line.services.fulfilment.{target}")
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _create_intent(**kwargs):
        return SimpleNamespace(id="pi_123", client_secret="pi_123_secret")

    def body(self, sim_type="esim", term="30D", promo="", **overrides):
        quote = build_quote(self.user, self.plan.pk, term, sim_type, promo)
        return {
            "quote_token": issue_token(self.user, quote),
            "switch_choice": "none",
            "billing": {"first_name": "Alice", "last_name": "Smith", "street": "1 High St",
                        "city": "London", "zip_code": "E1 1AA"},
            "contract": {"contract_version": "v1", "contract_summary_version": "s1", "accepted": True},
            **overrides,
        }

    def checkout(self, **kwargs):
        resp = self.client.post(CHECKOUT_URL, self.body(**kwargs), format="json")
        order = AddLineOrder.objects.filter(order_reference=resp.json().get("order_reference")).first()
        return resp, order

    def pay(self, order, service=None):
        service = service or fake_service()
        with mock.patch("apps.add_line.services.fulfilment.get_service", return_value=service):
            return payments.handle_payment_succeeded(order, fake_intent(order)), service


class StartCheckoutTests(CheckoutTestCase):
    def test_successful_checkout_holds_slot_and_sim(self):
        resp, order = self.checkout()

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["client_secret"], "pi_123_secret")
        self.assertEqual(order.payment_status, AddLineOrder.PaymentStatus.PENDING)
        self.assertEqual(order.fulfilment_status, AddLineOrder.FulfilmentStatus.SIM_RESERVED)
        self.assertEqual(order.hold.status, CapacityHold.Status.HELD)
        self.assertEqual(order.reserved_sim, self.sim)
        self.sim.refresh_from_db()
        self.assertEqual(self.sim.inventory, Sim.Inventory.PENDING)
        self.assertEqual(order.billing["email"], "alice@example.com")
        self.assertTrue(ContractAcceptance.objects.filter(order=order).exists())

        kwargs = self.create_intent.call_args.kwargs
        self.assertEqual(kwargs["amount"], 1000)
        self.assertEqual(kwargs["metadata"],
                         {"add_line_order": order.order_reference, "source": "zoiko_add_line"})

    def test_stale_login_requires_reauth(self):
        self.user.last_login = timezone.now() - timedelta(hours=2)
        self.user.save()
        resp, order = self.checkout()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "REAUTH_REQUIRED")
        self.assertFalse(CapacityHold.objects.exists())

    def test_outdated_contract_rejected(self):
        body = self.body()
        body["contract"]["contract_version"] = "old"
        resp = self.client.post(CHECKOUT_URL, body, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "CONTRACT_OUTDATED")

    def test_contract_must_be_accepted(self):
        body = self.body()
        body["contract"]["accepted"] = False
        self.assertEqual(self.client.post(CHECKOUT_URL, body, format="json").status_code, 400)

    def test_physical_sim_needs_delivery_address(self):
        make_sim(2, sim_type="UICC")
        resp, _ = self.checkout(sim_type="psim")
        self.assertEqual(resp.json()["code"], "DELIVERY_ADDRESS_REQUIRED")

        address = {"name": "Alice", "line1": "1 High St", "city": "London", "postcode": "E1 1AA"}
        resp, order = self.checkout(sim_type="psim", delivery_address=address)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(order.delivery_address["postcode"], "E1 1AA")

    def test_out_of_stock_releases_slot(self):
        self.sim.delete()
        resp, _ = self.checkout()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "OUT_OF_STOCK")
        self.assertEqual(CapacityHold.objects.get().status, CapacityHold.Status.RELEASED)

    def test_line_limit(self):
        for _ in range(5):
            Line.objects.create(owner=self.user, status=Line.Status.ACTIVE)
        resp, _ = self.checkout()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "LINE_LIMIT_REACHED")

    def test_stripe_failure_releases_everything(self):
        self.create_intent.side_effect = RuntimeError("stripe down")
        resp = self.client.post(CHECKOUT_URL, self.body(), format="json")
        self.assertEqual(resp.status_code, 502)
        order = AddLineOrder.objects.get()
        self.assertEqual(order.fulfilment_status, AddLineOrder.FulfilmentStatus.CANCELLED)
        self.assertEqual(order.hold.status, CapacityHold.Status.RELEASED)
        self.sim.refresh_from_db()
        self.assertEqual(self.sim.inventory, Sim.Inventory.IN_STOCK)

    def test_free_order_skips_stripe_and_activates(self):
        Coupon.objects.create(name="FREE", slug="free", discount=Decimal("100"),
                              type=Coupon.PERCENTAGE, valid_for_additional_lines=True)
        with mock.patch("apps.add_line.services.fulfilment.get_service", return_value=fake_service()):
            resp, order = self.checkout(promo="free")
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.json()["payment_required"])
        self.create_intent.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, AddLineOrder.PaymentStatus.PAID)
        self.assertEqual(order.fulfilment_status, AddLineOrder.FulfilmentStatus.ACTIVATED)


class PaymentAndFulfilmentTests(CheckoutTestCase):
    def test_payment_activates_line(self):
        _, order = self.checkout()
        order, service = self.pay(order)

        self.assertEqual(order.payment_status, AddLineOrder.PaymentStatus.PAID)
        self.assertEqual(order.fulfilment_status, AddLineOrder.FulfilmentStatus.ACTIVATED)
        line = order.line
        self.assertEqual((line.owner, line.status, line.current_msisdn),
                         (self.user, Line.Status.ACTIVE, "447700900001"))
        self.assertEqual(CapacityHold.objects.get(pk=order.hold_id).status, CapacityHold.Status.CONSUMED)
        self.sim.refresh_from_db()
        self.assertEqual(self.sim.inventory, Sim.Inventory.ACTIVATED)
        self.assertEqual(service.activate_order_sim.call_args.kwargs["package_code"], "TSL_UK_10GB")

        sim_order = SimOrder.objects.get(order_reference=order.order_reference)
        self.assertEqual(sim_order.user_id, str(self.user.pk))
        self.assertEqual(sim_order.status, SimOrder.STATUS_COMPLETED)

    def test_payment_is_idempotent(self):
        _, order = self.checkout()
        self.pay(order)
        _, service = self.pay(order)
        service.activate_order_sim.assert_not_called()
        self.assertEqual(Line.objects.count(), 1)

    def test_mismatched_amount_rejected(self):
        _, order = self.checkout()
        with self.assertRaises(payments.PaymentMismatch):
            payments.handle_payment_succeeded(order, fake_intent(order, amount=1, amount_received=1))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, AddLineOrder.PaymentStatus.PENDING)

    def test_coupon_use_recorded_once(self):
        coupon = Coupon.objects.create(name="EXTRA", slug="extra", discount=Decimal("2.00"),
                                       valid_for_additional_lines=True)
        _, order = self.checkout(promo="extra")
        self.pay(order)
        self.pay(order)
        coupon.refresh_from_db()
        self.assertEqual(coupon.used_count, 1)
        self.assertEqual(CouponUsage.objects.filter(coupon=coupon).count(), 1)

    def test_activation_failure_then_retry(self):
        _, order = self.checkout()
        order, _ = self.pay(order, fake_service(activate_error=TransatelAPIError("boom", status_code=500)))
        self.assertEqual(order.fulfilment_status, AddLineOrder.FulfilmentStatus.FAILED)
        self.assertEqual(order.line.status, Line.Status.PENDING_ACTIVATION)
        self.assertEqual(len(order.errors), 1)

        with mock.patch("apps.add_line.services.fulfilment.get_service", return_value=fake_service()):
            call_command("retry_add_line_fulfilment", stdout=StringIO())
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, AddLineOrder.FulfilmentStatus.ACTIVATED)
        self.assertEqual(Line.objects.count(), 1)

    def test_retries_stop_after_max_attempts(self):
        _, order = self.checkout()
        order, _ = self.pay(order, fake_service(activate_error=TransatelAPIError("boom")))
        failing = fake_service(activate_error=TransatelAPIError("boom"))
        with mock.patch("apps.add_line.services.fulfilment.get_service", return_value=failing):
            for _ in range(6):
                call_command("retry_add_line_fulfilment", stdout=StringIO())
        order.refresh_from_db()
        self.assertEqual(len(order.errors), 5)
        self.assertEqual(failing.activate_order_sim.call_count, 4)

    def test_still_provisioning_is_not_reported_live(self):
        _, order = self.checkout()
        order, _ = self.pay(order, fake_service(post_status="Available"))
        self.assertEqual(order.fulfilment_status, AddLineOrder.FulfilmentStatus.ACTIVATING)
        self.assertEqual(order.line.status, Line.Status.PENDING_ACTIVATION)

        later = fake_service(subscriber_status="Active")
        with mock.patch("apps.add_line.services.fulfilment.get_service", return_value=later):
            call_command("retry_add_line_fulfilment", stdout=StringIO())
        later.activate_order_sim.assert_not_called()  # never re-sent
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, AddLineOrder.FulfilmentStatus.ACTIVATED)
        self.assertEqual(order.line.status, Line.Status.ACTIVE)

    def test_lapsed_sim_reservation_is_replaced(self):
        _, order = self.checkout()
        SimReservation.objects.all().delete()
        Sim.objects.filter(pk=self.sim.pk).update(inventory=Sim.Inventory.ACTIVATED, order_reference="ZK-other")
        replacement = make_sim(3)

        order, _ = self.pay(order)
        self.assertEqual(order.reserved_sim, replacement)
        self.assertEqual(order.line.sim, replacement)


class ConfirmAndWebhookTests(CheckoutTestCase):
    def confirm(self, order, status="succeeded"):
        intent = fake_intent(order, status=status)
        with mock.patch("apps.add_line.views.stripe.PaymentIntent.retrieve", return_value=intent), \
                mock.patch("apps.add_line.services.fulfilment.get_service", return_value=fake_service()):
            return self.client.post(f"/api/v1/lines/orders/{order.order_reference}/confirm/")

    def test_confirm_marks_paid(self):
        _, order = self.checkout()
        resp = self.confirm(order)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["fulfilment_status"], "activated")
        self.assertEqual(resp.json()["message"], "Your Zoiko service is active.")
        self.assertEqual(resp.json()["line"]["msisdn"], "447700900001")

    def test_confirm_unpaid(self):
        _, order = self.checkout()
        resp = self.confirm(order, status="requires_payment_method")
        self.assertEqual(resp.status_code, 402)

    def test_other_customers_order_is_hidden(self):
        _, order = self.checkout()
        bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.client.force_authenticate(bob)
        self.assertEqual(self.client.get(f"/api/v1/lines/orders/{order.order_reference}/").status_code, 404)
        self.assertEqual(self.confirm(order).status_code, 404)

    def test_order_detail_before_payment(self):
        _, order = self.checkout()
        body = self.client.get(f"/api/v1/lines/orders/{order.order_reference}/").json()
        self.assertEqual(body["message"], "Waiting for payment.")
        self.assertIsNone(body["line"])

    def webhook(self, event):
        with mock.patch("apps.add_line.views.stripe.Webhook.construct_event", return_value=event), \
                mock.patch("apps.add_line.services.fulfilment.get_service", return_value=fake_service()):
            return APIClient().post("/api/v1/lines/webhooks/stripe/", b"{}",
                                    content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1")

    def test_webhook_marks_paid(self):
        _, order = self.checkout()
        resp = self.webhook({"type": "payment_intent.succeeded", "data": {"object": fake_intent(order)}})
        self.assertEqual(resp.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, AddLineOrder.FulfilmentStatus.ACTIVATED)

    def test_webhook_ignores_other_payments(self):
        resp = self.webhook({"type": "payment_intent.succeeded",
                             "data": {"object": {"metadata": {"source": "zoiko_checkout"}}}})
        self.assertEqual(resp.json()["ignored"], "not an add-line payment")

    def test_webhook_canceled_releases(self):
        _, order = self.checkout()
        self.webhook({"type": "payment_intent.canceled",
                      "data": {"object": fake_intent(order, status="canceled")}})
        order.refresh_from_db()
        self.assertEqual(order.fulfilment_status, AddLineOrder.FulfilmentStatus.CANCELLED)
        self.assertEqual(order.hold.status, CapacityHold.Status.RELEASED)

    @override_settings(STRIPE_ADD_LINE_WEBHOOK_SECRET="")
    def test_webhook_without_secret_refuses(self):
        resp = APIClient().post("/api/v1/lines/webhooks/stripe/", b"{}", content_type="application/json")
        self.assertEqual(resp.status_code, 503)


class ExpiryTests(CheckoutTestCase):
    def expire(self, order):
        CapacityHold.objects.filter(pk=order.hold_id).update(expires_at=timezone.now() - timedelta(minutes=1))

    def test_abandoned_checkout_is_cancelled_and_released(self):
        _, order = self.checkout()
        self.expire(order)
        with mock.patch("apps.add_line.services.checkout.stripe.PaymentIntent.cancel") as cancel:
            call_command("release_expired_line_holds", stdout=StringIO())
        cancel.assert_called_once_with("pi_123")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, AddLineOrder.PaymentStatus.FAILED)
        self.assertEqual(order.hold.status, CapacityHold.Status.RELEASED)
        self.sim.refresh_from_db()
        self.assertEqual(self.sim.inventory, Sim.Inventory.IN_STOCK)

    def test_payment_that_beat_expiry_is_honoured(self):
        _, order = self.checkout()
        self.expire(order)
        with mock.patch("apps.add_line.services.checkout.stripe.PaymentIntent.cancel",
                        side_effect=RuntimeError("already succeeded")), \
                mock.patch("apps.add_line.services.checkout.stripe.PaymentIntent.retrieve",
                           return_value=fake_intent(order)), \
                mock.patch("apps.add_line.services.fulfilment.get_service", return_value=fake_service()):
            call_command("release_expired_line_holds", stdout=StringIO())
        order.refresh_from_db()
        self.assertEqual(order.payment_status, AddLineOrder.PaymentStatus.PAID)
        self.assertEqual(order.fulfilment_status, AddLineOrder.FulfilmentStatus.ACTIVATED)

    def test_unreachable_stripe_does_not_release(self):
        _, order = self.checkout()
        self.expire(order)
        with mock.patch("apps.add_line.services.checkout.stripe.PaymentIntent.cancel",
                        side_effect=RuntimeError("network")), \
                mock.patch("apps.add_line.services.checkout.stripe.PaymentIntent.retrieve",
                           side_effect=RuntimeError("network")):
            call_command("release_expired_line_holds", stdout=StringIO())
        order.refresh_from_db()
        self.assertEqual(order.payment_status, AddLineOrder.PaymentStatus.PENDING)
