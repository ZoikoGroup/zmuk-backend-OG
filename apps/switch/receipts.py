"""Milestone 4, step B4: payment receipts and the "Your documents" card.

CTO document 03 (v1.0) rules, as implemented:

* A receipt exists only for a verified capture of more than £0 (PR-01..06):
  the PaymentIntent is fetched from Stripe server-side and must match the
  order (reference, GBP, amount captured = order total). A mismatch issues
  nothing and is flagged for ops (PR-10, PR-11).
* One capture → one receipt, keyed on Stripe's charge id, so a replayed or
  duplicate event never creates a second one (PR-07, PR-25).
* Amounts come from the money captured; the line items are the accepted
  order quote and must add up to it exactly, in pence (financial invariant).
* Decision of 16 Nov 2026: UK prices always include VAT, and receipts show
  no VAT details (no VAT number, no net/VAT split). "Not a VAT invoice".
* Refunds and disputes are separate linked records; the original receipt is
  never changed (PR-16, PR-17).
* Receipt emails are logged per attempt; "sent" only when the mail service
  accepted it (PR-27). Staff re-sends send the original bytes and are logged.
"""

import hashlib
import logging
from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.add_line.models import AddLineOrder, Term
from apps.add_line.services import checkout
from core.stripe_compat import to_plain

from . import documents, services
from .emails import send_html_email
from .models import (
    SwitchContractConsent,
    SwitchDocumentDelivery,
    SwitchPaymentAdjustment,
    SwitchPaymentCapture,
    SwitchPaymentReceipt,
    SwitchRequest,
)

logger = logging.getLogger("apps.switch")

RECEIPT_TEMPLATE_VERSION = "PR-1.0"
MAX_RECEIPT_ATTEMPTS = 5
PDF = "application/pdf"


class CaptureMismatch(Exception):
    pass


def _pence(amount) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1")))


def _gbp(minor: int) -> str:
    return f"£{minor // 100}.{minor % 100:02d}"


def _note(switch, text):
    stamp = timezone.now().strftime("%Y-%m-%d %H:%M")
    switch.ops_note = f"{switch.ops_note}\n[{stamp}] {text}".strip()
    switch.save(update_fields=["ops_note", "updated_at"])


# ── Recording the capture ─────────────────────────────────────────────────────

def _method_label(charge: dict) -> str:
    details = charge.get("payment_method_details") or {}
    card = details.get("card") or {}
    if card.get("last4"):
        return f"{str(card.get('brand') or 'Card').title()} ending {card['last4']}"
    kind = str(details.get("type") or "").replace("_", " ").strip()
    return kind.title() if kind else ""


def _fetch_charge(order: AddLineOrder) -> dict:
    """The captured charge behind the order, verified against it."""
    if not checkout.stripe_configured():
        raise CaptureMismatch("Stripe isn't configured")
    intent = to_plain(stripe.PaymentIntent.retrieve(order.stripe_payment_intent_id, expand=["latest_charge"]))
    charge = intent.get("latest_charge") or {}
    if isinstance(charge, str):
        charge = to_plain(stripe.Charge.retrieve(charge))
    problems = []
    if intent.get("status") != "succeeded":
        problems.append("intent status")
    if (intent.get("metadata") or {}).get("add_line_order") != order.order_reference:
        problems.append("order reference")
    if not charge.get("id") or not charge.get("captured"):
        problems.append("no captured charge")
    if (charge.get("currency") or "").lower() != "gbp":
        problems.append("currency")
    if charge.get("amount_captured") != _pence(order.total_gross):
        problems.append("amount")
    if problems:
        raise CaptureMismatch(", ".join(problems))
    return charge


def record_payment(order: AddLineOrder):
    """Record the capture for a paid switch order and issue its receipt.

    Returns the capture, or None when there is nothing to record (not a
    switch order, £0 order). Safe to call repeatedly.
    """
    switch = SwitchRequest.objects.filter(order=order).first()
    if switch is None or order.payment_status != AddLineOrder.PaymentStatus.PAID:
        return None
    if _pence(order.total_gross) == 0 or not order.stripe_payment_intent_id:
        return None  # "No payment was taken for this order" (PR-06)

    existing = SwitchPaymentCapture.objects.filter(order=order).first()
    if existing is None:
        try:
            charge = _fetch_charge(order)
        except CaptureMismatch as exc:
            logger.critical("Receipt withheld for %s: %s", order.order_reference, exc)
            _note(switch, f"Receipt withheld: payment record didn't match the order ({exc}).")
            return None
        try:
            with transaction.atomic():
                existing, _ = SwitchPaymentCapture.objects.get_or_create(
                    provider="stripe", provider_capture_id=charge["id"],
                    defaults={
                        "switch_request": switch, "order": order,
                        "payment_intent_id": order.stripe_payment_intent_id,
                        "amount_minor": charge["amount_captured"], "currency": "GBP",
                        "captured_at": datetime.fromtimestamp(int(charge.get("created") or 0), tz=dt_timezone.utc)
                        if charge.get("created") else timezone.now(),
                        "method_label": _method_label(charge),
                    },
                )
        except IntegrityError:
            existing = SwitchPaymentCapture.objects.get(provider="stripe", provider_capture_id=charge["id"])
    issue_receipt(existing)
    return existing


# ── Issuing the receipt ───────────────────────────────────────────────────────

def _line_items(order: AddLineOrder):
    q = order.quote or {}
    term = q.get("term")
    period = Term(term).label if term in Term.values else ""
    monthly = _pence(q.get("monthly_price", "0"))
    promo = q.get("promo") or {}
    discount = min(_pence(promo.get("discount", "0")), monthly) if promo else 0
    delivery = _pence(q.get("delivery_fee", "0"))
    items = [(f"{q.get('plan', {}).get('name', 'Mobile plan')} ({period}): first payment", "1", monthly)]
    if discount:
        items.append((f"Promotional discount ({promo.get('code', '')})", "—", -discount))
    if delivery:
        items.append(("SIM delivery", "1", delivery))
    return items


def receipt_content(capture: SwitchPaymentCapture, receipt_number: str) -> dict:
    order, switch = capture.order, capture.switch_request
    items = _line_items(order)
    if sum(amount for _, _, amount in items) != capture.amount_minor:
        raise CaptureMismatch("line items don't add up to the captured amount")
    q = order.quote or {}
    return {
        "receipt_number": receipt_number,
        "paid_at": documents._uk_time(capture.captured_at),
        "order_reference": order.order_reference,
        "request_reference": switch.reference,
        "customer": f"{switch.first_name} {switch.last_name}".strip(),
        "method": capture.method_label or "Online payment",
        # A safe public reference, not the processor's id (PR-33).
        "transaction_reference": "TX-" + hashlib.sha256(capture.provider_capture_id.encode()).hexdigest()[:10].upper(),
        "items": [(label, qty, _gbp(abs(a)) if a >= 0 else f"−{_gbp(-a)}") for label, qty, a in items],
        "total": _gbp(capture.amount_minor),
        "plan": q.get("plan", {}).get("name", ""),
        "term_label": q.get("term_label", ""),
        "sim_type": "eSIM" if q.get("sim_type") == "esim" else "Physical SIM",
        "support_email": "info@zoikomobile.co.uk",
    }


def issue_receipt(capture: SwitchPaymentCapture):
    """Create the receipt PDF once; failures are recorded and retried by
    `reconcile_switch_receipts`, never shown as a download (PR-26)."""
    if hasattr(capture, "receipt"):
        return capture.receipt
    number = SwitchPaymentReceipt.new_number()
    try:
        content = receipt_content(capture, number)
        pdf = documents.render_receipt(content)
        with transaction.atomic():
            receipt = SwitchPaymentReceipt.objects.create(
                capture=capture, receipt_number=number,
                filename=f"Zoiko_Mobile_Payment_Receipt_{number}.pdf",
                pdf=pdf, sha256=documents.sha256(pdf), size=len(pdf), template_version=RECEIPT_TEMPLATE_VERSION,
            )
            capture.receipt_status = SwitchPaymentCapture.ReceiptStatus.READY
            capture.receipt_error = ""
            capture.save(update_fields=["receipt_status", "receipt_error"])
    except IntegrityError:
        capture.refresh_from_db()
        return getattr(capture, "receipt", None)  # another worker issued it
    except Exception as exc:
        logger.exception("Receipt for capture %s failed", capture.public_id)
        capture.receipt_status = SwitchPaymentCapture.ReceiptStatus.FAILED
        capture.receipt_attempts += 1
        capture.receipt_error = str(exc)[:255]
        capture.save(update_fields=["receipt_status", "receipt_attempts", "receipt_error"])
        return None
    email_receipt(receipt)
    return receipt


def email_receipt(receipt: SwitchPaymentReceipt, actor=None, resend=False) -> bool:
    switch = receipt.capture.switch_request
    recipient = receipt.capture.order.owner.email or switch.email
    accepted = send_html_email(
        subject=f"Your Zoiko Mobile payment receipt {receipt.receipt_number}",
        template_name="emails/switch_receipt.html",
        context={
            "first_name": switch.first_name,
            "receipt_number": receipt.receipt_number,
            "total": _gbp(receipt.capture.amount_minor),
            "order_reference": receipt.capture.order.order_reference,
            "request_reference": switch.reference,
        },
        recipient=recipient,
        attachments=[(receipt.filename, bytes(receipt.pdf), PDF)],
    )
    SwitchDocumentDelivery.objects.create(
        receipt=receipt, recipient_masked=services.mask_email(recipient), accepted=accepted, resend=resend,
        actor=actor if actor is not None and getattr(actor, "is_authenticated", False) else None,
    )
    return accepted


# ── Refunds and disputes (Stripe charge events) ───────────────────────────────

def on_charge_event(sender, event, **kwargs):
    """add_line's Stripe webhook forwards verified charge.* events here."""
    obj = event["data"]["object"]
    kind = event["type"]
    charge_id = obj.get("charge") if kind.startswith("charge.dispute.") else obj.get("id")
    capture = SwitchPaymentCapture.objects.filter(provider="stripe", provider_capture_id=charge_id).first()
    if capture is None:
        return
    with transaction.atomic():
        capture = SwitchPaymentCapture.objects.select_for_update().get(pk=capture.pk)
        if kind == "charge.refunded":
            refunded = int(obj.get("amount_refunded") or 0)
            new = max(0, refunded - capture.refunded_minor)
            if not new:
                return
            adj_kind, amount = SwitchPaymentAdjustment.Kind.REFUND, new
            capture.refunded_minor = refunded
            capture.status = (SwitchPaymentCapture.Status.REFUNDED if refunded >= capture.amount_minor
                              else SwitchPaymentCapture.Status.PARTIALLY_REFUNDED)
        else:
            adj_kind, amount = SwitchPaymentAdjustment.Kind.DISPUTE, int(obj.get("amount") or 0)
            if kind == "charge.dispute.created":
                capture.status = SwitchPaymentCapture.Status.DISPUTED
        try:
            with transaction.atomic():
                SwitchPaymentAdjustment.objects.create(
                    capture=capture, kind=adj_kind, provider_event_id=event["id"], amount_minor=amount,
                    provider_status=str(obj.get("status") or "")[:40],
                    occurred_at=datetime.fromtimestamp(int(event.get("created") or 0), tz=dt_timezone.utc)
                    if event.get("created") else timezone.now(),
                )
        except IntegrityError:
            return  # replayed event
        capture.save(update_fields=["status", "refunded_minor"])
    _note(capture.switch_request, f"Stripe {kind} on order {capture.order.order_reference}: {_gbp(amount)}.")


# ── "Your documents" card ─────────────────────────────────────────────────────

def documents_card(switch: SwitchRequest) -> dict:
    """Post-order documents with honest payment wording (document 03 §07)."""
    order = switch.order
    consent = SwitchContractConsent.objects.select_related("pack").filter(order=order).first() if order else None
    rows = []
    if consent:
        for doc in sorted(consent.pack.documents.all(), key=lambda d: d.kind != "contract_summary"):
            rows.append({
                "kind": doc.kind, "title": doc.get_kind_display(), "available": True,
                "description": ("A copy of the one-page summary you received before confirming your order."
                                if doc.kind == "contract_summary"
                                else "The full plan and contract details you received before placing your order."),
                "filename": doc.filename, "sha256": doc.sha256,
                "download_url": f"/api/switch-requests/{switch.reference}/documents/{doc.public_id}/",
            })

    payment = {"state": "not_ordered", "message": "You haven't placed an order yet."}
    receipt_row = None
    if order:
        P = AddLineOrder.PaymentStatus
        capture = SwitchPaymentCapture.objects.filter(order=order).first()
        receipt = getattr(capture, "receipt", None) if capture else None
        if order.payment_status == P.PENDING:
            payment = {"state": "pending",
                       "message": "Payment pending. We'll provide a receipt once your payment has been confirmed."}
        elif order.payment_status == P.FAILED:
            payment = {"state": "failed", "message": "Payment not completed."}
        elif _pence(order.total_gross) == 0:
            payment = {"state": "no_payment", "message": "No payment was taken for this order."}
        elif capture and capture.status == SwitchPaymentCapture.Status.DISPUTED:
            payment = {"state": "disputed", "message": "Your payment status has changed. Please contact support."}
        elif capture and capture.status in (SwitchPaymentCapture.Status.REFUNDED,
                                            SwitchPaymentCapture.Status.PARTIALLY_REFUNDED):
            last = capture.adjustments.filter(kind=SwitchPaymentAdjustment.Kind.REFUND).last()
            when = documents._uk_time(last.occurred_at).split(",")[0] if last else ""
            payment = {"state": capture.status,
                       "message": (f"Refund issued on {when}. See your original payment receipt."
                                   if capture.status == SwitchPaymentCapture.Status.REFUNDED
                                   else f"A refund of {_gbp(capture.refunded_minor)} has been issued.")}
        elif receipt:
            payment = {"state": "paid", "message": "Payment received. Download your receipt."}
        else:
            payment = {"state": "preparing", "message": "Payment received. We're preparing your receipt."}

        if receipt:
            delivered = receipt.deliveries.filter(accepted=True).exists()
            receipt_row = {
                "kind": "payment_receipt", "title": "Payment receipt", "available": True,
                "description": f"A record of your confirmed payment. Paid on "
                               f"{documents._uk_time(capture.captured_at).split(',')[0]}.",
                "receipt_number": receipt.receipt_number, "filename": receipt.filename, "sha256": receipt.sha256,
                "download_url": f"/api/switch-requests/{switch.reference}/receipts/{receipt.public_id}/",
                "email_status": "sent" if delivered else "not_sent",
            }

    if receipt_row:
        rows.append(receipt_row)
    return {
        "reference": switch.reference,
        "order_reference": order.order_reference if order else None,
        "payment": payment,
        "documents": rows,
        "contract_email_status": consent.pack.email_status if consent else None,
        "email_masked": services.mask_email(switch.email),
    }


def get_receipt(switch: SwitchRequest, user, receipt_id):
    receipt = (
        SwitchPaymentReceipt.objects.select_related("capture__order")
        .filter(public_id=receipt_id, capture__switch_request=switch, capture__order__owner=user)
        .first()
    )
    if receipt is None or not receipt.bytes_intact():
        if receipt is not None:
            logger.critical("Receipt %s failed its integrity check", receipt.public_id)
        return None
    return receipt


# ── Reconciliation (daily) ────────────────────────────────────────────────────

def reconcile() -> dict:
    """Make sure every paid switch order with money taken has exactly one
    capture and one receipt; retry what's missing; report the rest."""
    report = {"captures_recorded": 0, "receipts_issued": 0, "still_missing": 0, "gave_up": 0}
    paid = AddLineOrder.objects.filter(
        payment_status=AddLineOrder.PaymentStatus.PAID, switch_request__isnull=False,
    ).exclude(total_gross=0)
    for order in paid:
        if not SwitchPaymentCapture.objects.filter(order=order).exists():
            if record_payment(order):
                report["captures_recorded"] += 1
            else:
                report["still_missing"] += 1
    for capture in SwitchPaymentCapture.objects.exclude(receipt_status=SwitchPaymentCapture.ReceiptStatus.READY):
        if capture.receipt_attempts >= MAX_RECEIPT_ATTEMPTS:
            report["gave_up"] += 1
            continue
        if issue_receipt(capture):
            report["receipts_issued"] += 1
        else:
            report["still_missing"] += 1
    return report
