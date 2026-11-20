"""Milestone 4, step B2: order preview and pre-contract documents.

Order of events required by CTO documents 01/02 (non-negotiable):

    plan & term → locked quote → Contract Information + Contract Summary
    generated and stored → provided to the customer (download + email) →
    express consent (B3) → payment (B3) → receipt (B4)

Only the signed-in owner of a READY_FOR_ORDER request can use these, and
only while Part B is enabled (continuation.enabled). Issued documents stay
downloadable by their owner afterwards.

If approved contract content is missing or can't be rendered, no document is
issued and no payment is possible. The request is parked as "Action needed —
internal" (reason contract_docs, owner operations), the fault is noted and
Operations are alerted. Ops reopen it with "ready for order" once Legal /
Product have fixed the content (Engineering Decision v2.1 §04).
"""

import logging
from datetime import timedelta

import stripe
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.add_line.models import AddLineOrder, SimType
from apps.add_line.services import checkout, payments
from apps.add_line.services.quote import QuoteError, build_quote
from core.stripe_compat import to_plain

from . import continuation, documents, services
from .emails import ops_recipient, send_html_email
from .models import (
    SwitchContractConsent,
    SwitchContractDocument,
    SwitchContractPack,
    SwitchContractProvision,
    SwitchRequest,
    SwitchStatusEvent,
)

logger = logging.getLogger("apps.switch")

S = SwitchRequest.Status
PDF = "application/pdf"

R = SwitchRequest.ActionReason

# Approved wording (Engineering Decision v2.1 §04, 19 Nov 2026).
DOCUMENTS_FAILED_MESSAGE = (
    "We can't complete your contract documents right now. Your request is saved. "
    "We'll contact you when you can continue."
)
_PRICE_KEYS = ("monthly_price", "promo", "delivery_fee", "charge_today", "recurring_amount", "currency")


class OrderError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _not_found():
    return OrderError("NOT_FOUND", "We couldn't find that switch request in your account.", 404)


# ── Access ────────────────────────────────────────────────────────────────────

def get_owned(reference: str, user) -> SwitchRequest:
    """The request if `user` owns it; the same 404 otherwise (no probing)."""
    switch = (
        SwitchRequest.objects.select_related("plan", "order", "line")
        .filter(reference=(reference or "").strip().upper(), user=user)
        .first()
    )
    if switch is None:
        raise _not_found()
    return switch


def _documents_unavailable():
    return OrderError("DOCUMENTS_UNAVAILABLE", DOCUMENTS_FAILED_MESSAGE, 503)


def _check_ready(switch: SwitchRequest):
    if not continuation.enabled():
        raise OrderError(continuation.NOT_AVAILABLE.code, continuation.NOT_AVAILABLE.message, 404)
    if switch.status == S.ACTION_NEEDED and switch.action_reason == R.CONTRACT_DOCS:
        # Parked by _hold_for_documents: keep showing the same safe message.
        raise _documents_unavailable()
    if switch.status != S.READY_FOR_ORDER:
        raise OrderError(continuation.NOT_READY.code, continuation.NOT_READY.message, 409)
    if switch.required_code_type and services.code_expired(switch):
        raise OrderError(
            "CODE_EXPIRED",
            f"Your {switch.required_code_type} has expired. Please send us a new one before you continue.",
            409,
        )


def _quote(switch: SwitchRequest, user):
    try:
        # New customer's line: the additional-line promo rule doesn't apply.
        return build_quote(user, switch.plan_id, switch.contract_term, switch.sim_type, switch.promo_code,
                           additional_line=False)
    except QuoteError as exc:
        raise OrderError("QUOTE_UNAVAILABLE", f"{exc.message} Please contact support to update your request.", 409)


def _price(quote_dict: dict) -> dict:
    return {k: quote_dict.get(k) for k in _PRICE_KEYS}


# ── Preview ───────────────────────────────────────────────────────────────────

def preview(switch: SwitchRequest, user) -> dict:
    """What the review screen shows before documents are generated."""
    _check_ready(switch)
    quote = _quote(switch, user).as_dict()
    submitted = (switch.quote_snapshot or {}).get("quote") or {}
    sources = documents.find_sources(switch.plan_id, switch.contract_term)
    return {
        "reference": switch.reference,
        "plan": quote["plan"],
        "contract_term": switch.contract_term,
        "sim_type": switch.sim_type,
        "switch_route": switch.switch_route,
        "port_number_masked": services.mask_number(switch.port_number),
        "pac_status": services.pac_status(switch),
        "billing_address": {
            "address_line_1": switch.address_line_1,
            "address_line_2": switch.address_line_2,
            "town_city": switch.town_city,
            "postcode": switch.postcode,
        },
        "quote": quote,
        # Price differs from what was shown when the request was submitted.
        "price_changed": bool(submitted) and _price(submitted) != _price(quote),
        "submitted_price": _price(submitted) if submitted else None,
        "documents_available": not documents.missing_content(sources),
        "documents": pack_data(current_pack(switch, user)),
    }


# ── Documents ─────────────────────────────────────────────────────────────────

def current_pack(switch: SwitchRequest, user):
    """The newest pack that can still be agreed to, or None."""
    return (
        SwitchContractPack.objects.filter(
            switch_request=switch, customer=user, superseded_at__isnull=True,
            offer_valid_until__gt=timezone.now(),
        )
        .prefetch_related("documents")
        .first()
    )


def pack_data(pack):
    if pack is None:
        return None
    ref = pack.switch_request.reference
    consent = getattr(pack, "consent", None)
    return {
        "id": str(pack.public_id),
        "contract_reference": pack.reference,
        "quote": pack.quote,
        "offer_valid_until": pack.offer_valid_until,
        "email_status": pack.email_status,
        "consented": consent is not None,
        "consented_at": consent.consented_at if consent else None,
        "documents": [
            {
                "id": str(doc.public_id),
                "kind": doc.kind,
                "title": doc.get_kind_display(),
                "filename": doc.filename,
                "content_type": PDF,
                "size": doc.size,
                "sha256": doc.sha256,
                "template_version": doc.template_version,
                "download_url": f"/api/switch-requests/{ref}/documents/{doc.public_id}/",
            }
            for doc in sorted(pack.documents.all(), key=lambda d: d.kind != "contract_summary")
        ],
    }


def _hold_for_documents(switch: SwitchRequest, fault: str) -> None:
    """Park the request as an internal action and alert Operations, once.

    `fault` names missing content fields / render errors only (no personal
    data); it goes to the log, the ops note and the ops email, never to the
    customer. Repeat attempts while parked don't alert again.
    """
    logger.error("Contract documents blocked for %s: %s", switch.reference, fault)
    with transaction.atomic():
        locked = SwitchRequest.objects.select_for_update().get(pk=switch.pk)
        if locked.status != S.READY_FOR_ORDER:
            return
        locked.set_status(S.ACTION_NEEDED, reason=R.CONTRACT_DOCS, owner=SwitchRequest.ActionOwner.OPERATIONS)
        stamp = timezone.now().strftime("%Y-%m-%d %H:%M")
        locked.ops_note = f"{locked.ops_note}\n[{stamp}] Contract documents blocked — {fault}".strip()
        locked.save(update_fields=["ops_note", "updated_at"])
    switch.status, switch.action_reason, switch.action_owner = locked.status, locked.action_reason, locked.action_owner

    alerted = send_html_email(
        subject=f"Action needed: contract documents blocked for {locked.reference}",
        template_name="emails/switch_ops_alert.html",
        context={
            "title": "Contract documents blocked",
            "summary": ("A customer tried to continue their switch but their contract documents could not be "
                        "issued. No document was issued and no payment was taken. The request is saved and "
                        "waiting on Operations."),
            "reference": locked.reference,
            "selected_plan": locked.selected_plan,
            "contract_term": locked.get_contract_term_display() if locked.contract_term else "",
            "fault": fault,
            "next_step": ("Ask Legal / Product to complete and approve the contract terms and plan tariff in the "
                          "admin, then move the request to 'ready for order' (this emails the customer a new "
                          "link)."),
            "admin_url": settings.BACKEND_URL.rstrip("/") + reverse(
                "admin:switch_switchrequest_change", args=[locked.pk]),
        },
        recipient=ops_recipient(),
    )
    if not alerted:
        logger.error("Ops alert for blocked contract documents on %s was not sent", locked.reference)


def issue_documents(switch: SwitchRequest, user) -> SwitchContractPack:
    """Freeze the quote and issue both PDFs (or return the still-valid pack
    for the same quote and content versions, so the bytes don't change
    under the customer)."""
    _check_ready(switch)
    quote = _quote(switch, user)
    sources = documents.find_sources(switch.plan_id, switch.contract_term)
    missing = documents.missing_content(sources)
    if missing:
        _hold_for_documents(switch, "missing approved content: " + ", ".join(missing))
        raise _documents_unavailable()

    existing = current_pack(switch, user)
    if (existing and existing.quote_hash == quote.hash and existing.terms_version == sources.terms.version
            and existing.tariff_version == sources.tariff.version):
        return existing

    now = timezone.now()
    reference = SwitchContractPack.new_reference()
    valid_until = now + timedelta(minutes=settings.SWITCH_DOCUMENTS_VALID_MINUTES)
    try:
        content = documents.build_content(
            switch, quote.as_dict(), sources, pack_reference=reference, issued_at=now,
            offer_valid_until=valid_until,
        )
        summary = documents.render_summary(content)
        information = documents.render_information(content)
    except (documents.MissingContent, documents.TooLong) as exc:
        _hold_for_documents(switch, f"documents could not be rendered: {exc}")
        raise _documents_unavailable()

    with transaction.atomic():
        SwitchContractPack.objects.filter(
            switch_request=switch, superseded_at__isnull=True,
        ).update(superseded_at=now)
        pack = SwitchContractPack.objects.create(
            reference=reference, switch_request=switch, customer=user,
            quote=quote.as_dict(), quote_hash=quote.hash,
            terms_version=sources.terms.version, tariff_version=sources.tariff.version,
            content=content, offer_valid_until=valid_until,
        )
        for kind, pdf, template in (
            (SwitchContractDocument.Kind.CONTRACT_SUMMARY, summary, documents.SUMMARY_TEMPLATE_VERSION),
            (SwitchContractDocument.Kind.CONTRACT_INFORMATION, information,
             documents.INFORMATION_TEMPLATE_VERSION),
        ):
            name = "Contract_Summary" if kind == SwitchContractDocument.Kind.CONTRACT_SUMMARY else "Contract_Information"
            SwitchContractDocument.objects.create(
                pack=pack, kind=kind, filename=f"Zoiko_Mobile_{name}_{reference}.pdf",
                pdf=pdf, sha256=documents.sha256(pdf), size=len(pdf), template_version=template,
            )
        # Both PDFs are now in the customer's account to view and download.
        SwitchContractProvision.objects.create(
            pack=pack, method=SwitchContractProvision.Method.ACCOUNT, succeeded=True, actor=user,
        )

    _email_documents(pack)
    return pack


def _provided(pack: SwitchContractPack):
    """Successful provision events if, together, they cover every document
    in the pack; else None."""
    events = list(pack.provisions.filter(succeeded=True).select_related("document"))
    needed = {d.pk for d in pack.documents.all()}
    covered = set()
    for event in events:
        covered |= needed if event.document_id is None else {event.document_id}
    return events if needed and covered >= needed else None


def _email_documents(pack: SwitchContractPack):
    """Durable copy by email (CI-02). The outcome is recorded; the customer is
    only told "sent" when the mail service accepted it (CI-20)."""
    switch = pack.switch_request
    docs = list(pack.documents.all())
    sent = send_html_email(
        subject=f"Your Zoiko Mobile contract documents ({pack.reference})",
        template_name="emails/switch_documents.html",
        context={
            "first_name": switch.first_name,
            "contract_reference": pack.reference,
            "request_reference": switch.reference,
            "plan": pack.quote["plan"]["name"],
            "offer_valid_until": documents._uk_time(pack.offer_valid_until),
        },
        recipient=pack.customer.email or switch.email,
        attachments=[(d.filename, bytes(d.pdf), PDF) for d in docs],
    )
    pack.email_status = SwitchContractPack.EmailStatus.SENT if sent else SwitchContractPack.EmailStatus.FAILED
    pack.email_sent_at = timezone.now() if sent else None
    pack.save(update_fields=["email_status", "email_sent_at"])
    SwitchContractProvision.objects.create(
        pack=pack, method=SwitchContractProvision.Method.EMAIL, succeeded=sent,
        recipient_masked=services.mask_email(pack.customer.email or switch.email),
    )


def get_document(switch: SwitchRequest, user, document_id, *, ip_address=None,
                 user_agent="") -> SwitchContractDocument:
    """An issued PDF for its owner, checked against its stored hash."""
    doc = (
        SwitchContractDocument.objects.select_related("pack")
        .filter(public_id=document_id, pack__switch_request=switch, pack__customer=user)
        .first()
    )
    if doc is None:
        raise _not_found()
    if not doc.bytes_intact():
        logger.critical("Contract document %s failed its integrity check", doc.public_id)
        raise OrderError("DOCUMENT_UNAVAILABLE",
                         "We couldn't open this document. Please contact support.", 500)
    SwitchContractProvision.objects.create(
        pack=doc.pack, document=doc, method=SwitchContractProvision.Method.DOWNLOAD, succeeded=True,
        actor=user, ip_address=ip_address or None, user_agent=(user_agent or "")[:255],
    )
    return doc


# ── B3: consent and checkout ──────────────────────────────────────────────────

# Approved wording (CTO document 02, Part B §05). Bump the version with any change.
CONSENT_STATEMENT = (
    "I confirm that I have received the Contract Information and Contract Summary and agree to "
    "enter into the contract on the terms shown."
)
CONSENT_STATEMENT_VERSION = "CI-1.0/consent-1"

_ROUTE_SWITCH_CHOICE = {
    SwitchRequest.Route.KEEP_PAC: AddLineOrder.SwitchChoice.PAC,
    SwitchRequest.Route.NEW_STAC: AddLineOrder.SwitchChoice.STAC,
    SwitchRequest.Route.NEW_KEEP: AddLineOrder.SwitchChoice.NONE,
}


def _commercial(quote_dict: dict) -> dict:
    """What the customer agreed to pay for; the renewal date moves daily."""
    return {"plan": (quote_dict.get("plan") or {}).get("id"), "term": quote_dict.get("term"),
            "sim_type": quote_dict.get("sim_type"), **_price(quote_dict)}


def _open_pack(switch, user, pack_id):
    pack = (
        SwitchContractPack.objects.prefetch_related("documents")
        .filter(public_id=pack_id, switch_request=switch, customer=user)
        .first()
    )
    if pack is None:
        raise _not_found()
    if pack.superseded_at or pack.offer_valid_until <= timezone.now():
        raise OrderError("DOCUMENTS_EXPIRED", "These documents have expired or been replaced. "
                         "Please review the latest documents.", 409)
    return pack


def consent(switch: SwitchRequest, user, *, pack_id, documents_given, immediate_service_requested=False,
            ip_address=None, user_agent="") -> SwitchContractPack:
    """Record express consent to one pack. `documents_given` is the list of
    {id, sha256} the customer was shown; it must match the issued pack
    exactly. Repeating it for the same pack is harmless."""
    _check_ready(switch)
    with transaction.atomic():
        pack = _open_pack(switch, user, pack_id)
        expected = {str(d.public_id): d.sha256 for d in pack.documents.all()}
        given = {str(d.get("id")): str(d.get("sha256", "")).lower() for d in documents_given}
        if given != expected:
            raise OrderError("DOCUMENTS_MISMATCH", "These aren't the documents we issued to you. "
                             "Please review the latest documents and try again.", 409)
        if not SwitchContractConsent.objects.filter(pack=pack).exists():
            # Both documents must have been provided before consent (T04).
            provided = _provided(pack)
            if provided is None:
                raise OrderError("DOCUMENTS_NOT_PROVIDED", "We couldn't confirm your contract documents were "
                                 "provided to you. Please review the latest documents and try again.", 409)
            SwitchContractConsent.objects.create(
                pack=pack, customer=user, statement=CONSENT_STATEMENT,
                statement_version=CONSENT_STATEMENT_VERSION, documents=expected,
                provision=[event.as_evidence() for event in provided],
                immediate_service_requested=bool(immediate_service_requested),
                consented_at=timezone.now(), ip_address=ip_address or None,
                user_agent=(user_agent or "")[:255],
            )
            SwitchStatusEvent.objects.create(
                switch_request=switch, kind=SwitchStatusEvent.Kind.CONSENTED, status_before=switch.status,
                status_after=switch.status, actor=user, note=pack.reference,
            )
    return SwitchContractPack.objects.prefetch_related("documents").get(pk=pack.pk)


def _billing(switch, user):
    street = ", ".join(p for p in (switch.address_line_1, switch.address_line_2) if p)
    return {"first_name": switch.first_name, "last_name": switch.last_name, "house_number": "",
            "street": street, "city": switch.town_city, "zip_code": switch.postcode,
            "email": user.email or switch.email}


def _delivery(switch):
    if switch.sim_type != SimType.PSIM:
        return None
    return {"name": f"{switch.first_name} {switch.last_name}".strip(), "line1": switch.address_line_1,
            "line2": switch.address_line_2, "city": switch.town_city, "postcode": switch.postcode}


def _checkout_result(order, client_secret):
    return {
        "order_reference": order.order_reference,
        "client_secret": client_secret,
        "payment_required": client_secret is not None,
        "amount": str(order.total_gross),
        "currency": order.currency,
        # Call after stripe.confirmPayment() succeeds (the webhook also confirms).
        "confirm_url": f"/api/v1/lines/orders/{order.order_reference}/confirm/",
    }


def _resume(order):
    """An unpaid checkout already exists: hand back its PaymentIntent."""
    if not checkout.stripe_configured():
        raise OrderError("PAYMENTS_UNAVAILABLE", "Payments are not available right now.", 503)
    try:
        intent = to_plain(stripe.PaymentIntent.retrieve(order.stripe_payment_intent_id))
    except Exception:
        logger.exception("Could not retrieve PaymentIntent for %s", order.order_reference)
        raise OrderError("PAYMENT_CHECK_FAILED", "We couldn't load your payment. Please try again.", 502)
    return _checkout_result(order, intent.get("client_secret"))


def start_checkout(switch: SwitchRequest, user, *, ip_address=None, user_agent="") -> dict:
    """Create the order and Stripe PaymentIntent for a consented pack.

    The order goes through the existing add_line checkout (SIM and slot
    held, Stripe PaymentIntent, webhook/confirm, activation, transfer).
    Calling again while that checkout is open returns the same payment.
    """
    if not continuation.enabled():
        raise OrderError(continuation.NOT_AVAILABLE.code, continuation.NOT_AVAILABLE.message, 404)

    free_order = None
    with transaction.atomic():
        switch = SwitchRequest.objects.select_for_update().get(pk=switch.pk)
        order = switch.order
        if order and order.payment_status == AddLineOrder.PaymentStatus.PAID:
            raise OrderError("ALREADY_ORDERED", "You've already placed this order.", 409)
        if (order and order.payment_status == AddLineOrder.PaymentStatus.PENDING
                and order.fulfilment_status != AddLineOrder.FulfilmentStatus.CANCELLED
                and order.stripe_payment_intent_id):
            return _resume(order)

        _check_ready(switch)
        pack = current_pack(switch, user)
        agreed = SwitchContractConsent.objects.filter(pack=pack).first() if pack else None
        if agreed is None:
            raise OrderError("CONSENT_REQUIRED", "Please review and agree to your contract documents first.", 409)

        quote = _quote(switch, user)
        if _commercial(quote.as_dict()) != _commercial(pack.quote):
            raise OrderError("PRICE_CHANGED", "The price has changed since your documents were issued. "
                             "Please review your new contract documents.", 409)

        try:
            order, client_secret = checkout.checkout_quote(
                user, quote,
                switch_choice=_ROUTE_SWITCH_CHOICE[switch.switch_route],
                billing=_billing(switch, user),
                delivery_address=_delivery(switch),
                contract={
                    # add_line's own acceptance record; the full evidence is
                    # the SwitchContractConsent on the pack.
                    "contract_version": settings.ADD_LINE_CONTRACT_VERSION,
                    "contract_summary_version": settings.ADD_LINE_CONTRACT_SUMMARY_VERSION,
                    "documents_hash": documents.sha256("".join(sorted(agreed.documents.values())).encode()),
                    "immediate_service_requested": agreed.immediate_service_requested,
                },
                ip_address=ip_address, user_agent=user_agent,
                metadata={"switch_request": switch.reference, "contract_reference": pack.reference},
            )
        except checkout.CheckoutError as exc:
            raise OrderError(exc.code, exc.message, exc.status)

        switch.order = order
        switch.save(update_fields=["order", "updated_at"])
        agreed.order = order
        agreed.save(update_fields=["order"])
        SwitchStatusEvent.objects.create(
            switch_request=switch, kind=SwitchStatusEvent.Kind.CHECKOUT_STARTED, status_before=switch.status,
            status_after=switch.status, actor=user, note=order.order_reference,
        )
        if client_secret is None:
            free_order = order

    if free_order is not None:
        # £0 (100% promo): no Stripe; paid at once, which converts the request.
        payments.mark_free_order_paid(free_order)
    return _checkout_result(order, client_secret)
