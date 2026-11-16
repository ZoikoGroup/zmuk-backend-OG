"""Contract Summary and Contract Information PDFs (CTO documents 01 and 02, v1.0).

* Every value comes from the frozen quote, the request, the approved
  ContractTerms (Legal) and the approved PlanTariff (Product). Nothing is
  taken from the public site or filled with a default.
* A missing value stops issue (`MissingContent`): no "TBC", no £0.00, no
  placeholder ever reaches a customer (CS release rule, CI-13/CI-14).
* The summary must fit one A4 page at 10 pt or larger (CS-03); if the
  approved wording is too long it fails closed rather than spilling over.
* No PAC/STAC, card data or unrelated personal data is printed (CS-08, CI-19).
"""

import hashlib
import io
from dataclasses import dataclass
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.add_line.models import SimType, Term

from .models import ContractTerms, ContractTermRule, PlanTariff, SwitchRequest

SUMMARY_TEMPLATE_VERSION = "CS-1.0"
INFORMATION_TEMPLATE_VERSION = "CI-1.0"

_TERMS_FIELDS = [
    "legal_name", "company_number", "registered_office", "support_phone", "support_email", "website",
    "customer_care_hours", "complaints_route", "complaints_code", "adr_scheme", "compensation",
    "device", "service_availability", "quality_and_sla", "traffic_management", "performance_remedies",
    "payment_method", "billing_start", "usage_controls", "provisioning",
    "cancellation", "cooling_off", "prepaid_credit_refund",
    "data_categories", "privacy_policy", "security_incidents", "directory_listing",
    "emergency_access", "accessibility",
]
_RULE_FIELDS = ["minimum_commitment", "renewal", "early_termination"]
_TARIFF_FIELDS = [
    "uk_data", "uk_minutes", "uk_texts", "fair_use", "after_allowance", "rollover",
    "roaming_zone", "roaming_allowance", "roaming_rates", "speeds", "extra_charges",
    "one_off_charges", "scheduled_price_changes",
]

_ROUTE_TEXT = {
    SwitchRequest.Route.KEEP_PAC: "Keep your number: transfer with a PAC",
    SwitchRequest.Route.NEW_STAC: "New Zoiko number: end your old service with a STAC (your old number is not kept)",
    SwitchRequest.Route.NEW_KEEP: "New Zoiko number: no transfer; your current service carries on",
}


class MissingContent(Exception):
    """Something the documents need isn't approved or is blank."""

    def __init__(self, missing):
        super().__init__("missing contract content: " + ", ".join(missing))
        self.missing = missing


class TooLong(Exception):
    """The one-page summary doesn't fit on one page."""


@dataclass
class Sources:
    terms: ContractTerms | None
    rule: ContractTermRule | None
    tariff: PlanTariff | None


def find_sources(plan_id, term) -> Sources:
    terms = ContractTerms.current()
    rule = terms.term_rules.filter(term=term).first() if terms else None
    tariff = PlanTariff.objects.filter(plan_id=plan_id, approved=True).first()
    return Sources(terms, rule, tariff)


def missing_content(sources: Sources) -> list[str]:
    """Names of everything blank or unapproved, for ops (never shown to customers)."""
    missing = []
    if not sources.terms:
        missing.append("approved ContractTerms")
    else:
        missing += [f"terms.{f}" for f in _TERMS_FIELDS if not str(getattr(sources.terms, f) or "").strip()]
        if not sources.rule:
            missing.append("terms rule for this plan length")
        else:
            missing += [f"rule.{f}" for f in _RULE_FIELDS if not (getattr(sources.rule, f) or "").strip()]
    if not sources.tariff:
        missing.append("approved PlanTariff for this plan")
    else:
        missing += [f"tariff.{f}" for f in _TARIFF_FIELDS if not (getattr(sources.tariff, f) or "").strip()]
    return missing


# ── Content ───────────────────────────────────────────────────────────────────

def _period(term):
    return "per 30 days" if term == Term.DAYS_30 else "per month"


def _gbp(value):
    return f"£{value}"


_UK = ZoneInfo("Europe/London")


def _uk_time(dt):
    # UK civil time with GMT/BST handled (PR-31), whatever the server zone is.
    return timezone.localtime(dt, _UK).strftime("%d %B %Y, %H:%M %Z")


def _national(msisdn):
    return "0" + msisdn[2:] if msisdn and msisdn.startswith("44") else msisdn


def build_content(switch: SwitchRequest, quote: dict, sources: Sources, *, pack_reference,
                  issued_at, offer_valid_until) -> dict:
    missing = missing_content(sources)
    if missing:
        raise MissingContent(missing)
    terms, rule, tariff = sources.terms, sources.rule, sources.tariff
    term = quote["term"]
    promo = quote.get("promo")
    promo_text = (
        f"{_gbp(promo['discount'])} off your first payment (promo code {promo['code']}); "
        f"then {_gbp(quote['recurring_amount'])} {_period(term)}."
        if promo else "None."
    )
    transfer = ""
    if switch.switch_route != SwitchRequest.Route.NEW_KEEP and switch.port_number:
        transfer = _national(switch.port_number)

    return {
        "issuer": {f: getattr(terms, f) for f in _TERMS_FIELDS[:6]},
        "terms": {f: getattr(terms, f) for f in _TERMS_FIELDS[6:]},
        "terms_version": terms.version,
        "rule": {f: getattr(rule, f) for f in _RULE_FIELDS},
        "tariff": {f: getattr(tariff, f) for f in _TARIFF_FIELDS},
        "tariff_version": tariff.version,
        "customer": {
            "name": f"{switch.first_name} {switch.last_name}".strip(),
            # Full consumer protection unless a verified business opt-out exists (CI-06).
            "category": "Consumer",
        },
        "plan": {
            "name": quote["plan"]["name"],
            "term": term,
            "term_label": Term(term).label,
            "sim_type": SimType(quote["sim_type"]).label,
            "switch_option": _ROUTE_TEXT[switch.switch_route],
            "switch_route": switch.switch_route,
            "number_to_transfer": transfer,
        },
        "price": {
            "currency": quote["currency"],
            "core": f"{_gbp(quote['monthly_price'])} {_period(term)}",
            "promotion": promo_text,
            "delivery": _gbp(quote["delivery_fee"]),
            "first_payment": _gbp(quote["charge_today"]),
            "first_payment_vat": _gbp(quote["vat_included"]),
            "vat_rate_percent": quote["vat_rate_percent"],
            "first_renewal_date": quote["first_renewal_date"],
        },
        "references": {
            "contract_reference": pack_reference,
            "request_reference": switch.reference,
            "issued_at": _uk_time(issued_at),
            "offer_valid_until": _uk_time(offer_valid_until),
        },
    }


# ── PDF ───────────────────────────────────────────────────────────────────────

_INK = colors.HexColor("#0e1929")
_BRAND = colors.HexColor("#e6007e")

_BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=12.5, textColor=_INK, alignment=TA_LEFT)
_SMALL = ParagraphStyle("small", parent=_BODY, fontSize=10, leading=12, textColor=colors.HexColor("#444444"))
_H1 = ParagraphStyle("h1", parent=_BODY, fontName="Helvetica-Bold", fontSize=18, leading=22, spaceAfter=4)
_H2 = ParagraphStyle("h2", parent=_BODY, fontName="Helvetica-Bold", fontSize=11.5, leading=14,
                     spaceBefore=7, spaceAfter=2, textColor=_BRAND)
_BULLET = ParagraphStyle("bullet", parent=_BODY, leftIndent=10, bulletIndent=0)


def _p(text, style=_BODY):
    # Approved wording is plain text: escape it so nothing is read as markup.
    return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)


def _rich(html, style=_BODY):
    return Paragraph(html, style)


def _b(label, value):
    return _rich(f"<b>{escape(label)}</b> {escape(str(value))}".replace("\n", "<br/>"))


def _rows(pairs):
    table = Table([[_rich(f"<b>{escape(k)}</b>"), _p(v)] for k, v in pairs], colWidths=[48 * mm, None])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#d9dde3")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _issuer_line(c):
    i = c["issuer"]
    return _p(f"{i['legal_name']}  |  Company number {i['company_number']}  |  {i['registered_office']}\n"
              f"{i['support_phone']}  |  {i['support_email']}  |  {i['website']}", _SMALL)


def _render(story, *, title, author, subject) -> tuple[bytes, int]:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, title=title, author=author, subject=subject, creator=author, lang="en-GB",
        leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
    )
    doc.build(story)
    return buf.getvalue(), doc.page


def render_summary(c: dict) -> bytes:
    plan, price, t, tariff, rule, ref = c["plan"], c["price"], c["terms"], c["tariff"], c["rule"], c["references"]
    story = [
        _issuer_line(c),
        _p(f"Dated: {ref['issued_at']}", _SMALL),
        Spacer(1, 4),
        _p("Contract summary", _H1),
        # Prescribed introductory statements (Implementing Regulation (EU) 2019/2243).
        _rich("• This contract summary provides the main elements of this service offer as required by "
              "EU law <super>(1)</super>.", _BULLET),
        _p("• It helps to make a comparison between service offers.", _BULLET),
        _p("• Complete information about the service is provided in other documents.", _BULLET),
        _p("Services and equipment", _H2),
        _b("Selected mobile service:", f"{plan['name']}  |  Plan duration: {plan['term_label']}."),
        _b("UK allowance each billing period:",
           f"Data: {tariff['uk_data']}; minutes: {tariff['uk_minutes']}; texts: {tariff['uk_texts']}. "
           f"{tariff['fair_use']}"),
        _b("Roaming:", f"{tariff['roaming_zone']} {tariff['roaming_allowance']}"),
        _b("SIM type:", f"{plan['sim_type']}.  Device: {t['device']}"),
        _p("Speeds of the internet service and remedies", _H2),
        _b("Mobile data:", tariff["speeds"]),
        _b("Remedies:", t["performance_remedies"]),
        _p("Price", _H2),
        _b("Core subscription (including VAT):", price["core"]),
        _b("One-off charges:", f"{tariff['one_off_charges']}  Delivery: {price['delivery']}."),
        _b("Promotion:", price["promotion"]),
        _b("Scheduled core subscription changes:", tariff["scheduled_price_changes"]),
        _b("Out-of-allowance, premium, international and roaming charges:", tariff["extra_charges"]),
        _p("Duration, renewal and termination", _H2),
        _b("Minimum commitment:", rule["minimum_commitment"]),
        _b("At the end of the commitment:", rule["renewal"]),
        _b("Cancelling / switching:", t["cancellation"]),
        _b("Early termination charges:", rule["early_termination"]),
        _b("Distance-selling cancellation rights:", t["cooling_off"]),
        _p("Features for end-users with disabilities", _H2),
        _p(t["accessibility"]),
        _p("Other relevant information", _H2),
        _b("Switch option:", plan["switch_option"]),
        _b("Contract reference:", f"{ref['contract_reference']}  |  Request: {ref['request_reference']}"),
        _b("Offer valid until:", ref["offer_valid_until"]),
        _b("Full Contract Information:", f"issued with this summary under reference {ref['contract_reference']}."),
        Spacer(1, 4),
        _rich("<super>(1)</super> Commission Implementing Regulation (EU) 2019/2243, as referenced by Ofcom.",
              _SMALL),
    ]
    pdf, pages = _render(story, title="Contract summary", author=c["issuer"]["legal_name"],
                         subject=f"Contract summary {ref['contract_reference']}")
    if pages != 1:
        raise TooLong(f"contract summary is {pages} pages; it must fit one A4 page")
    return pdf


def render_information(c: dict) -> bytes:
    i, t, tariff, rule, plan, price, ref, cust = (
        c["issuer"], c["terms"], c["tariff"], c["rule"], c["plan"], c["price"], c["references"], c["customer"])
    if plan["switch_route"] == SwitchRequest.Route.KEEP_PAC:
        switching = (f"Your number {plan['number_to_transfer']} will move to {i['legal_name']} once your valid "
                     "PAC has been submitted and the transfer has been confirmed.")
    elif plan["switch_route"] == SwitchRequest.Route.NEW_STAC:
        switching = ("Your service moves to Zoiko with a new number; your old number is not kept. Your old "
                     f"service{' on ' + plan['number_to_transfer'] if plan['number_to_transfer'] else ''} "
                     "ends once the switch has been confirmed.")
    else:
        switching = "You're getting a new number. No transfer of your old number is requested."

    story = [
        _issuer_line(c),
        Spacer(1, 4),
        _p("Contract information", _H1),
        _p("Detailed information about your plan, charges, service and your rights. This document and your "
           "separate one-page Contract Summary form part of your contract.", _SMALL),

        _p("01  Who provides your service", _H2),
        _p(f"Provider: {i['legal_name']}. Company number: {i['company_number']}. Registered office: "
           f"{i['registered_office']}."),
        _p(f"Customer support: {i['support_phone']}  |  Email: {i['support_email']}  |  Website: {i['website']}."),
        _b("Contact channels and hours:", t["customer_care_hours"]),

        _p("02  Your contract and mobile service", _H2),
        _rows([
            ("Contract reference", ref["contract_reference"]),
            ("Request reference", ref["request_reference"]),
            ("Date provided", ref["issued_at"]),
            ("Customer", f"{cust['name']} ({cust['category']})"),
            ("Selected plan", plan["name"]),
            ("Contract duration", rule["minimum_commitment"]),
            ("SIM", plan["sim_type"]),
            ("Number arrangement", plan["switch_option"]),
            ("Device / equipment", t["device"]),
        ]),
        _b("Service availability:", t["service_availability"]),

        _p("03  Allowances, speeds and quality", _H2),
        _rows([
            ("Calls", tariff["uk_minutes"]),
            ("Texts", tariff["uk_texts"]),
            ("Mobile data", tariff["uk_data"]),
            ("Fair use and limits", tariff["fair_use"]),
            ("After your allowance", tariff["after_allowance"]),
            ("Rollover", tariff["rollover"]),
            ("Speeds", tariff["speeds"]),
            ("Traffic management", t["traffic_management"]),
            ("Quality / SLA", t["quality_and_sla"]),
            ("Remedies", t["performance_remedies"]),
        ]),

        _p("Charges and payment", _H2),
        _rows([
            ("Core recurring subscription", f"{price['core']}, including VAT"),
            ("Promotion", price["promotion"]),
            ("Dated subscription changes", tariff["scheduled_price_changes"]),
            ("One-off costs", f"{tariff['one_off_charges']} Delivery: {price['delivery']}."),
            ("First payment", f"{price['first_payment']} including VAT of {price['first_payment_vat']} "
                              f"({price['vat_rate_percent']}%)."),
            ("Payment and billing", f"{t['payment_method']} {t['billing_start']}"),
            ("Extra usage and service charges", tariff["extra_charges"]),
        ]),

        _p("04  Roaming and travel", _H2),
        _b("Included destinations:", tariff["roaming_zone"]),
        _b("Roaming allowance:", tariff["roaming_allowance"]),
        _b("Outside the zone or allowance:", tariff["roaming_rates"]),

        _p("05  Price transparency and billing controls", _H2),
        _p(t["usage_controls"]),

        _p("06  Delivery, activation and switching", _H2),
        _b("Provisioning:", t["provisioning"]),
        _p(switching),
        _p("You get a PAC from your current provider by texting PAC to 65075, or a STAC by texting STAC to "
           "75075. Codes normally remain valid for 30 days. Switching should normally be completed within one "
           "working day of the qualifying switch request under the applicable process; we will only give you "
           "a date once our systems confirm it. A completed order is not proof of activation or of a "
           "completed number transfer. Your old provider may charge lawful early-termination fees; text INFO "
           "to 85075 to find out."),

        _p("07  Duration, renewal and ending the agreement", _H2),
        _rows([
            ("Minimum commitment", rule["minimum_commitment"]),
            ("After the commitment", rule["renewal"]),
            ("Cancelling or moving provider", t["cancellation"]),
            ("Early termination charge", rule["early_termination"]),
            ("Remaining credit on switching", t["prepaid_credit_refund"]),
            ("Cooling-off (distance selling)", t["cooling_off"]),
        ]),

        _p("08  Security, personal data and privacy", _H2),
        _b("Personal data we use:", t["data_categories"]),
        _b("Privacy policy:", t["privacy_policy"]),
        _b("Security incidents:", t["security_incidents"]),
        _b("Directory listing:", t["directory_listing"]),

        _p("09  Accessibility and emergency access", _H2),
        _p(t["accessibility"]),
        _p("Contract Information and Contract Summary can be requested in an accessible format without charge."),
        _b("Emergency calls:", t["emergency_access"]),

        _p("10  Complaints, refunds and independent redress", _H2),
        _b("How to complain:", t["complaints_route"]),
        _b("Complaints Code:", t["complaints_code"]),
        _b("Independent dispute resolution:", t["adr_scheme"]),
        _b("Compensation and refunds:", t["compensation"]),

        _p("11  Your contract documents", _H2),
        _p(f"You received Contract Information template {INFORMATION_TEMPLATE_VERSION} and Contract Summary "
           f"template {SUMMARY_TEMPLATE_VERSION} (terms version {c['terms_version']}, tariff version "
           f"{c['tariff_version']}) under contract reference {ref['contract_reference']} on {ref['issued_at']}, "
           "before agreeing to your purchase. You can download the original PDFs from your account."),
    ]
    pdf, _ = _render(story, title="Contract information", author=i["legal_name"],
                     subject=f"Contract information {ref['contract_reference']}")
    return pdf


_ISSUER = ("Issued by Zoiko Mobile UK Ltd (trading as Zoiko Mobile UK), company no. 16564980; "
           "167–169 Great Portland Street, 5th Floor, London W1W 5PF. Support: 020 7164 6399 • "
           "info@zoikomobile.co.uk.")


def render_receipt(c: dict) -> bytes:
    """One-page payment receipt (CTO document 03, Part A). No VAT details by
    decision of 16 Nov 2026: prices include VAT; not a VAT invoice."""
    items = Table(
        [[_rich("<b>Item</b>"), _rich("<b>Qty</b>"), _rich("<b>Amount</b>")]]
        + [[_p(label), _p(qty), _p(amount)] for label, qty, amount in c["items"]]
        + [[_rich("<b>TOTAL PAYMENT RECEIVED</b>"), "", _rich(f"<b>{escape(c['total'])}</b>")]],
        colWidths=[None, 18 * mm, 32 * mm],
    )
    items.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, _INK),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#d9dde3")),
        ("LINEABOVE", (0, -1), (-1, -1), 0.6, _INK),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story = [
        _p("ZOIKO MOBILE UK", _H2),
        _p("Payment received", _H1),
        _p("Thank you. We have received the payment shown below."),
        Spacer(1, 6),
        _rows([
            ("Receipt reference", c["receipt_number"]),
            ("Payment date and time", c["paid_at"]),
            ("Order reference", f"{c['order_reference']} (switch request {c['request_reference']})"),
            ("Customer", c["customer"]),
            ("Payment method", c["method"]),
            ("Transaction reference", c["transaction_reference"]),
            ("Payment status", "PAID — capture confirmed"),
        ]),
        _p("What you paid", _H2),
        items,
        Spacer(1, 4),
        _p("All prices include VAT.", _SMALL),
        _p("About your service", _H2),
        _p(f"Plan: {c['plan']} ({c['term_label']})   •   SIM: {c['sim_type']}"),
        _p("Your receipt confirms payment only. It does not confirm that your SIM is active or that your "
           "mobile number has transferred. You can follow your switch in your Zoiko Mobile UK account."),
        Spacer(1, 8),
        _p(_ISSUER, _SMALL),
        _p(f"This is proof of payment, not a VAT invoice. If you need a VAT invoice, contact "
           f"{c['support_email']}.", _SMALL),
    ]
    pdf, pages = _render(story, title="Payment receipt", author="Zoiko Mobile UK Ltd",
                         subject=f"Payment receipt {c['receipt_number']}")
    if pages != 1:
        raise TooLong(f"receipt is {pages} pages")
    return pdf


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
