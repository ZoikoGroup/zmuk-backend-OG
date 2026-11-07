import hashlib
import hmac
import json
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.add_line.models import AddLineOrder, Line, PortRequest, SupplierEvent
from apps.add_line.services import pac, porting
from apps.add_line.services.fulfilment import fulfil
from apps.plans.models import Plan
from apps.sims.models import Sim
from apps.sims.transatel import TransatelAPIError

KEY = Fernet.generate_key().decode()
SECRET = "tsl-stream-test-7d3e91a0c4b2"
S = PortRequest.Status


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def port_service(tx="tx-port-1", error=None):
    service = mock.Mock()
    if error:
        service.request_uk_port_in.side_effect = error
    else:
        service.request_uk_port_in.return_value = {"transactionId": tx, "transactionStatus": "PENDING"}
    return service


class PacFormatTests(TestCase):
    def test_codes(self):
        self.assertEqual(pac.normalise_code("PAC", " abc 123456 "), "ABC123456")
        self.assertEqual(pac.normalise_code("STAC", "123456abc"), "123456ABC")
        for code_type, bad in (("PAC", "123456ABC"), ("STAC", "ABC123456"), ("PAC", "AB1234567")):
            with self.assertRaises(pac.PacError):
                pac.normalise_code(code_type, bad)

    def test_numbers(self):
        for raw in ("07700 900123", "+44 7700 900123", "00447700900123", "447700900123"):
            self.assertEqual(pac.normalise_msisdn(raw), "447700900123")
        for bad in ("01632 960000", "0770090012", ""):
            with self.assertRaises(pac.PacError):
                pac.normalise_msisdn(bad)

    @override_settings(PAC_ENCRYPTION_KEY=KEY)
    def test_encryption_round_trip(self):
        token = pac.encrypt("ABC123456")
        self.assertNotIn("ABC123456", token)
        self.assertEqual(pac.decrypt(token), "ABC123456")

    @override_settings(PAC_ENCRYPTION_KEY="")
    def test_missing_key(self):
        with self.assertRaises(pac.PacStorageUnavailable):
            pac.encrypt("ABC123456")


@override_settings(PAC_ENCRYPTION_KEY=KEY, TRANSATEL_WEBHOOK_SECRET=SECRET, PAC_RETENTION_DAYS=7,
                   PORT_STALE_HOURS=48, ADD_LINE_SUPPLIER_WRITES_ENABLED=True)
class PortingTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.plan = Plan.objects.create(name="Plan", price=Decimal("10"), duration_days=30, transatelID="TSL")
        self.sim = Sim.objects.create(iccid="8944000000000000001", serial_number="SER1", msisdn="447700900001")

    def make_line(self, status=Line.Status.ACTIVE, switch_choice="pac"):
        line = Line.objects.create(owner=self.user, sim=self.sim, plan=self.plan, status=status,
                                   current_msisdn="447700900001")
        AddLineOrder.objects.create(
            owner=self.user, line=line, plan=self.plan, term="30D", sim_type="esim",
            switch_choice=switch_choice, total_gross=Decimal("10"), vat_amount=Decimal("1.67"),
            payment_status="paid",
        )
        return line

    def request(self, line, service=None, code_type="PAC", code="ABC123456", msisdn="07700900123", **kw):
        service = service or port_service()
        with mock.patch("apps.add_line.services.porting.get_service", return_value=service):
            return porting.create_request(self.user, line, code_type=code_type, code=code,
                                          msisdn=msisdn, **kw), service


class CreateAndSubmitTests(PortingTestCase):
    def test_waits_until_line_active(self):
        port, service = self.request(self.make_line(status=Line.Status.PENDING_ACTIVATION))
        self.assertEqual(port.status, S.AWAITING_ACTIVATION)
        self.assertNotIn("ABC123456", port.encrypted_code)
        self.assertEqual(port.portability_msisdn, "447700900123")
        service.request_uk_port_in.assert_not_called()

    def test_active_line_submits_immediately(self):
        port, service = self.request(self.make_line(), wished_date=timezone.localdate() + timedelta(days=5))
        self.assertEqual(port.status, S.PENDING)  # received, NOT accepted
        self.assertEqual(port.supplier_transaction_id, "tx-port-1")
        args = service.request_uk_port_in.call_args.args
        self.assertEqual(args[:4], ("SER1", "447700900123", "PAC", "ABC123456"))

    def test_stac_only_when_chosen(self):
        with self.assertRaises(porting.PortError) as ctx:
            self.request(self.make_line(switch_choice="pac"), code_type="STAC", code="123456ABC")
        self.assertEqual(ctx.exception.code, "SWITCH_NOT_SELECTED")

    def test_no_switch_choice_rejects_codes(self):
        with self.assertRaises(porting.PortError) as ctx:
            self.request(self.make_line(switch_choice="none"))
        self.assertEqual(ctx.exception.code, "SWITCH_NOT_SELECTED")

    def test_one_open_transfer_per_line(self):
        line = self.make_line()
        self.request(line)
        with self.assertRaises(porting.PortError) as ctx:
            self.request(line)
        self.assertEqual(ctx.exception.code, "PORT_IN_PROGRESS")

    def test_past_wished_date_rejected(self):
        with self.assertRaises(porting.PortError) as ctx:
            self.request(self.make_line(), wished_date=date(2020, 1, 1))
        self.assertEqual(ctx.exception.code, "WISHED_DATE_INVALID")

    @override_settings(PAC_ENCRYPTION_KEY="")
    def test_no_encryption_key_refuses_code(self):
        with self.assertRaises(porting.PortError) as ctx:
            self.request(self.make_line())
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("PORTING_UNAVAILABLE", 503))
        self.assertFalse(PortRequest.objects.exists())

    def test_supplier_rejects_request(self):
        error = TransatelAPIError("bad msisdn", status_code=400, response={"detail": "bad msisdn"})
        port, _ = self.request(self.make_line(), port_service(error=error))
        self.assertEqual(port.status, S.ERROR)
        self.assertEqual(port.last_error["status_code"], 400)

    def test_supplier_outage_is_retried(self):
        line = self.make_line()
        port, _ = self.request(line, port_service(error=TransatelAPIError("down", status_code=503)))
        self.assertEqual(port.status, S.AWAITING_ACTIVATION)

        with mock.patch("apps.add_line.services.porting.get_service", return_value=port_service()):
            call_command("reconcile_port_requests", stdout=StringIO())
        port.refresh_from_db()
        self.assertEqual(port.status, S.PENDING)
        self.assertIsNone(port.last_error)

    @override_settings(ADD_LINE_SUPPLIER_WRITES_ENABLED=False)
    def test_supplier_writes_off_holds_transfer(self):
        line = self.make_line()
        port, service = self.request(line)
        service.request_uk_port_in.assert_not_called()
        self.assertEqual(port.status, S.AWAITING_ACTIVATION)
        self.assertNotEqual(port.encrypted_code, "")

        with override_settings(ADD_LINE_SUPPLIER_WRITES_ENABLED=True), \
                mock.patch("apps.add_line.services.porting.get_service", return_value=port_service()):
            call_command("reconcile_port_requests", stdout=StringIO())
        port.refresh_from_db()
        self.assertEqual(port.status, S.PENDING)

    def test_line_activation_sends_waiting_transfer(self):
        line = self.make_line(status=Line.Status.PENDING_ACTIVATION)
        port, _ = self.request(line)
        order = line.order
        order.reserved_sim = self.sim
        order.fulfilment_status = AddLineOrder.FulfilmentStatus.ACTIVATING
        order.save()
        Sim.objects.filter(pk=self.sim.pk).update(inventory=Sim.Inventory.ACTIVATED, order_reference=order.order_reference)

        network = mock.Mock()
        network.get_subscriber_by_serial.return_value = {"status": "Active"}
        with mock.patch("apps.add_line.services.fulfilment.get_service", return_value=network), \
                mock.patch("apps.add_line.services.fulfilment.deliver_qr"), \
                mock.patch("apps.add_line.services.porting.get_service", return_value=port_service()):
            fulfil(order)
        port.refresh_from_db()
        self.assertEqual(port.status, S.PENDING)


class StatusTests(PortingTestCase):
    def setUp(self):
        super().setUp()
        self.line = self.make_line()
        self.port, _ = self.request(self.line)

    def test_forward_only(self):
        self.assertTrue(porting.apply_status(self.port, "VALIDATED", port_date=date(2026, 12, 1)))
        self.assertFalse(porting.apply_status(self.port, "PENDING"))  # late event
        self.port.refresh_from_db()
        self.assertEqual((self.port.status, self.port.port_date), (S.VALIDATED, date(2026, 12, 1)))
        self.assertIsNotNone(self.port.validated_at)

    def test_unknown_status_ignored(self):
        self.assertFalse(porting.apply_status(self.port, "SOMETHING_NEW"))

    def test_done_switches_number_for_pac(self):
        porting.apply_status(self.port, "LOCKED")
        porting.apply_status(self.port, "DONE")
        self.line.refresh_from_db()
        self.assertEqual((self.line.current_msisdn, self.line.original_msisdn), ("447700900123", "447700900001"))
        self.sim.refresh_from_db()
        self.assertEqual(self.sim.msisdn, "447700900123")
        self.assertFalse(porting.apply_status(self.port, "INVALIDATED"))  # terminal stays terminal

    def test_invalidated_allows_new_code_and_keeps_line(self):
        porting.apply_status(self.port, "INVALIDATED")
        self.line.refresh_from_db()
        self.assertEqual((self.line.status, self.line.current_msisdn), (Line.Status.ACTIVE, "447700900001"))
        new_port, _ = self.request(self.line, code="XYZ654321")
        self.assertEqual(new_port.status, S.PENDING)


PORT_IN = "CONNECTIVITY-MANAGEMENT/PORTABILITY/UK/PORT_IN/"
SWITCH_IN = "CONNECTIVITY-MANAGEMENT/PORTABILITY/UK/SWITCH_IN/"


class WebhookTests(PortingTestCase):
    URL = "/api/v1/lines/webhooks/transatel/"

    def setUp(self):
        super().setUp()
        self.line = self.make_line()
        self.port, _ = self.request(self.line)

    def post(self, payload, signature=None, content_type="application/json"):
        body = json.dumps(payload).encode()
        return APIClient().post(self.URL, body, content_type=content_type,
                                HTTP_X_TSL_SIGNATURE_256=signature if signature is not None else sign(body))

    def event(self, event_id="evt-1", event_type=PORT_IN + "VALIDATED", **body):
        return {"header": {"eventId": event_id, "eventType": event_type, "eventDate": "2026-11-03T10:00:00Z"},
                "body": {"transactionId": "tx-port-1", **body}}

    def test_every_catalogue_port_in_event_maps(self):
        for i, (suffix, expected) in enumerate((
            ("ATTACH/REQUESTED", S.ATTACH_REQUESTED), ("ATTACH/RECEIVED", S.ATTACH_RECEIVED),
            ("VALIDATED", S.VALIDATED), ("DONE", S.DONE),
        )):
            self.post(self.event(f"e{i}", PORT_IN + suffix, simSerial="SER1"))
            self.port.refresh_from_db()
            self.assertEqual(self.port.status, expected, suffix)

    def test_terminal_catalogue_events(self):
        for suffix, expected in (("INVALIDATED", S.INVALIDATED), ("CANCELED", S.CANCELED),
                                 ("ATTACH/FAILED", S.ATTACH_FAILED)):
            PortRequest.objects.filter(pk=self.port.pk).update(status=S.PENDING)
            self.post(self.event(f"t-{suffix}", PORT_IN + suffix))
            self.port.refresh_from_db()
            self.assertEqual(self.port.status, expected, suffix)

    def test_switch_in_events_apply_to_stac_only(self):
        self.post(self.event("s1", SWITCH_IN + "VALIDATED"))
        self.assertIn("does not match", SupplierEvent.objects.get(event_id="s1").error)
        self.port.refresh_from_db()
        self.assertEqual(self.port.status, S.PENDING)

        PortRequest.objects.filter(pk=self.port.pk).update(code_type=PortRequest.CodeType.STAC)
        self.post(self.event("s2", SWITCH_IN + "VALIDATED"))
        self.port.refresh_from_db()
        self.assertEqual(self.port.status, S.VALIDATED)

    def test_sim_serial_mismatch_quarantined(self):
        self.post(self.event(simSerial="SOMEONE-ELSE"))
        event = SupplierEvent.objects.get()
        self.assertEqual((event.status, event.error),
                         (SupplierEvent.Status.IGNORED, "simSerial does not match the request's SIM"))
        self.port.refresh_from_db()
        self.assertEqual(self.port.status, S.PENDING)

    def test_unknown_event_type_stored_not_applied(self):
        for i, event_type in enumerate((PORT_IN + "LOCKED", "CONNECTIVITY-MANAGEMENT/PORT_IN/VALIDATED")):
            self.assertEqual(self.post(self.event(f"u{i}", event_type)).status_code, 204)
        self.assertEqual(set(SupplierEvent.objects.values_list("status", flat=True)),
                         {SupplierEvent.Status.IGNORED})
        self.port.refresh_from_db()
        self.assertEqual(self.port.status, S.PENDING)

    def test_wrong_content_type_rejected(self):
        self.assertEqual(self.post(self.event(), content_type="text/plain").status_code, 415)
        self.assertFalse(SupplierEvent.objects.exists())

    def test_oversized_body_rejected(self):
        self.assertEqual(self.post(self.event(padding="x" * 70_000)).status_code, 413)
        self.assertFalse(SupplierEvent.objects.exists())

    @override_settings(DEBUG=False, TRANSATEL_WEBHOOK_SECRET="changeme-please-123")
    def test_placeholder_secret_refuses(self):
        self.assertEqual(self.post(self.event()).status_code, 503)

    def test_valid_event_applied(self):
        resp = self.post(self.event(portabilityDate="2026-11-20"))
        self.assertEqual(resp.status_code, 204)
        self.port.refresh_from_db()
        self.assertEqual((self.port.status, self.port.port_date), (S.VALIDATED, date(2026, 11, 20)))
        self.assertEqual(SupplierEvent.objects.get().status, SupplierEvent.Status.PROCESSED)

    def test_bad_signature_rejected(self):
        self.assertEqual(self.post(self.event(), signature="sha256=deadbeef").status_code, 401)
        self.assertFalse(SupplierEvent.objects.exists())

    @override_settings(TRANSATEL_WEBHOOK_SECRET="")
    def test_no_secret_refuses(self):
        self.assertEqual(self.post(self.event(), signature="x").status_code, 503)

    def test_duplicate_delivery_processed_once(self):
        self.post(self.event())
        with mock.patch("apps.add_line.webhooks.process") as process:
            self.assertEqual(self.post(self.event()).status_code, 204)
        process.assert_not_called()
        self.assertEqual(SupplierEvent.objects.count(), 1)

    def test_malformed_payload(self):
        self.assertEqual(self.post({"no": "header"}).status_code, 400)

    def test_unmatched_event_ignored(self):
        self.post(self.event(transactionId="other-tx"))
        self.assertEqual(SupplierEvent.objects.get().status, SupplierEvent.Status.IGNORED)

    def test_out_of_order_event_ignored(self):
        porting.apply_status(self.port, "LOCKED", source="poll")  # LOCKED is polled; no event exists
        self.post(self.event("e2", PORT_IN + "VALIDATED"))
        self.port.refresh_from_db()
        self.assertEqual(self.port.status, S.LOCKED)

    def test_subscriber_activated_rechecks_supplier(self):
        order = self.line.order
        Line.objects.filter(pk=self.line.pk).update(status=Line.Status.PENDING_ACTIVATION)
        order.fulfilment_status = AddLineOrder.FulfilmentStatus.ACTIVATING
        order.reserved_sim = self.sim
        order.save()
        with mock.patch("apps.add_line.webhooks.fulfilment.fulfil") as fulfil_mock:
            self.post(self.event("e3", "CONNECTIVITY-MANAGEMENT/SUBSCRIBER/ACTIVATED", simSerial="SER1"))
        fulfil_mock.assert_called_once()


class PortApiTests(PortingTestCase):
    def setUp(self):
        super().setUp()
        self.line = self.make_line(status=Line.Status.PENDING_ACTIVATION)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f"/api/v1/lines/{self.line.uuid}/port/"

    def test_get_before_and_after(self):
        self.assertEqual(self.client.get(self.url).json(), {"port_request": None})
        resp = self.client.post(self.url, {"code_type": "PAC", "code": "abc123456", "msisdn": "07700900123"},
                                format="json")
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["status"], "AWAITING_ACTIVATION")
        self.assertNotIn("ABC123456", json.dumps(body))
        self.assertEqual(self.client.get(self.url).json()["port_request"]["id"], body["id"])

    def test_error_shape(self):
        resp = self.client.post(self.url, {"code_type": "PAC", "code": "nope", "msisdn": "07700900123"},
                                format="json")
        self.assertEqual((resp.status_code, resp.json()["code"]), (400, "PAC_FORMAT_INVALID"))

    def test_other_customer_cannot_see_line(self):
        self.client.force_authenticate(User.objects.create_user("bob", "bob@example.com", "pw"))
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_pac_format_check(self):
        resp = self.client.post("/api/v1/lines/pac/check/",
                                {"code_type": "PAC", "code": "ABC123456", "msisdn": "07700 900123"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["format_ok"])
        self.assertNotIn("verified", resp.json()["message"].lower())


class PurgeTests(PortingTestCase):
    def test_finished_and_expired_codes_removed(self):
        line = self.make_line()
        done, _ = self.request(line)
        porting.apply_status(done, "DONE")
        PortRequest.objects.filter(pk=done.pk).update(updated_at=timezone.now() - timedelta(days=8))

        waiting_line = Line.objects.create(owner=self.user, status=Line.Status.PENDING_ACTIVATION)
        waiting = PortRequest.objects.create(line=waiting_line, code_type="PAC",
                                             encrypted_code=pac.encrypt("ABC123456"))
        PortRequest.objects.filter(pk=waiting.pk).update(created_at=timezone.now() - timedelta(days=38))

        fresh_line = Line.objects.create(owner=self.user, status=Line.Status.PENDING_ACTIVATION)
        fresh = PortRequest.objects.create(line=fresh_line, code_type="PAC",
                                           encrypted_code=pac.encrypt("ABC123456"))

        out = StringIO()
        call_command("purge_pac_codes", stdout=out)
        self.assertIn("Purged 2", out.getvalue())

        done.refresh_from_db()
        waiting.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(done.encrypted_code, "")
        self.assertIsNotNone(done.code_purged_at)
        self.assertEqual((waiting.encrypted_code, waiting.status), ("", S.ERROR))
        self.assertNotEqual(fresh.encrypted_code, "")
