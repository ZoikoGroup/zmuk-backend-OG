from rest_framework import serializers

from .models import AddLineOrder, Line, LineClaim, PortRequest, SimType, Term
from .services.eligibility import MESSAGES


class EligibilitySerializer(serializers.Serializer):
    """Shape of GET /api/v1/lines/eligibility/ (built from `eligibility.Eligibility`)."""

    def to_representation(self, result):
        return {
            "eligible": result.eligible,
            "reasons": [{"code": code, "message": MESSAGES[code]} for code in result.reasons],
            "requires_reauth": result.requires_reauth,
            "lines": {
                "active": result.usage.lines,
                "in_progress": result.usage.holds,
                "limit": result.usage.limit,
                "remaining": result.usage.remaining,
            },
        }


class QuoteRequestSerializer(serializers.Serializer):
    plan_id = serializers.IntegerField()
    term = serializers.ChoiceField(choices=Term.choices)
    sim_type = serializers.ChoiceField(choices=SimType.choices)
    promo_code = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")


class BillingSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    house_number = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    street = serializers.CharField(max_length=200)
    city = serializers.CharField(max_length=100)
    zip_code = serializers.CharField(max_length=20)
    email = serializers.EmailField(required=False, allow_blank=True, default="")


class DeliveryAddressSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    line1 = serializers.CharField(max_length=200)
    line2 = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    city = serializers.CharField(max_length=100)
    postcode = serializers.CharField(max_length=20)


class ContractSerializer(serializers.Serializer):
    contract_version = serializers.CharField(max_length=32)
    contract_summary_version = serializers.CharField(max_length=32)
    documents_hash = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    accepted = serializers.BooleanField()
    immediate_service_requested = serializers.BooleanField(default=False)

    def validate_accepted(self, value):
        if not value:
            raise serializers.ValidationError("You must accept the contract to continue.")
        return value


class CheckoutRequestSerializer(serializers.Serializer):
    quote_token = serializers.CharField()
    switch_choice = serializers.ChoiceField(choices=AddLineOrder.SwitchChoice.choices)
    billing = BillingSerializer()
    delivery_address = DeliveryAddressSerializer(required=False, allow_null=True, default=None)
    contract = ContractSerializer()


def _order_message(order: AddLineOrder) -> str:
    """Customer wording per §09 — payment, activation and line are separate."""
    F = AddLineOrder.FulfilmentStatus
    if order.fulfilment_status == F.CANCELLED:
        return "This order was cancelled."
    if order.payment_status != AddLineOrder.PaymentStatus.PAID:
        if order.last_payment_error:
            return "Your last payment attempt didn't go through. You can try again."
        return "Waiting for payment."
    if order.fulfilment_status == F.ACTIVATED:
        return "Your Zoiko service is active."
    if order.fulfilment_status == F.FAILED:
        return "We're finishing setting up your SIM. Our team has been notified and will be in touch."
    return "Payment received — preparing your SIM."


class PortCodeSerializer(serializers.Serializer):
    code_type = serializers.ChoiceField(choices=PortRequest.CodeType.choices)
    code = serializers.CharField(max_length=20)
    msisdn = serializers.CharField(max_length=24)


class PortRequestCreateSerializer(PortCodeSerializer):
    wished_date = serializers.DateField(required=False, allow_null=True, default=None)


_PORT_MESSAGES = {
    PortRequest.Status.AWAITING_ACTIVATION: "We'll send your transfer request as soon as your new SIM is active.",
    PortRequest.Status.PENDING: "We have received your transfer request.",
    PortRequest.Status.ATTACH_REQUESTED: "We have received your transfer request.",
    PortRequest.Status.ATTACH_RECEIVED: "We have received your transfer request.",
    PortRequest.Status.VALIDATED: "Your number transfer is confirmed for the date shown.",
    PortRequest.Status.LOCKED: "Your number transfer is being prepared.",
    PortRequest.Status.DONE: "Your number has been transferred.",
    PortRequest.Status.INVALIDATED: (
        "We couldn't validate your transfer details. Please check your code with your "
        "previous provider and send it again — your new number keeps working meanwhile."
    ),
    PortRequest.Status.ATTACH_FAILED: "We need to help you finish switching. Our team will be in touch.",
    PortRequest.Status.ERROR: "We need to help you finish switching. Our team will be in touch.",
    PortRequest.Status.CANCELING: "Your transfer is being cancelled.",
    PortRequest.Status.CANCELED: "Your transfer was cancelled.",
}


class PortRequestSerializer(serializers.ModelSerializer):
    """Customer view of a transfer. The code itself is never returned."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    msisdn = serializers.CharField(source="portability_msisdn", read_only=True)
    message = serializers.SerializerMethodField()
    can_resubmit = serializers.SerializerMethodField()

    class Meta:
        model = PortRequest
        fields = (
            "id", "code_type", "msisdn", "status", "message", "wished_date", "port_date",
            "can_resubmit", "submitted_at", "completed_at", "created_at",
        )

    def get_message(self, port):
        return _PORT_MESSAGES[port.status]

    def get_can_resubmit(self, port):
        return port.is_terminal and port.status != PortRequest.Status.DONE


class LineSerializer(serializers.ModelSerializer):
    """One line on GET /api/v1/lines/ (the dashboard's "My mobile numbers")."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    msisdn = serializers.SerializerMethodField()
    plan = serializers.SerializerMethodField()
    sim = serializers.SerializerMethodField()
    order_reference = serializers.SerializerMethodField()
    port_request = serializers.SerializerMethodField()

    class Meta:
        model = Line
        fields = (
            "id", "msisdn", "status", "source", "plan", "term", "sim", "order_reference",
            "port_request", "activated_at", "created_at",
        )

    def get_msisdn(self, line):
        return line.current_msisdn or None

    def get_plan(self, line):
        plan = line.plan
        return {"id": plan.pk, "name": plan.name, "data_allowance": plan.data_allowance or ""} if plan else None

    def get_sim(self, line):
        sim = line.sim
        if not sim:
            return None
        return {"type": sim.sim_type_key, "iccid_last4": sim.iccid[-4:]}

    def get_order_reference(self, line):
        order = getattr(line, "order", None)
        return order.order_reference if order else None

    def get_port_request(self, line):
        # Prefetched newest-first by the view.
        ports = list(line.port_requests.all())
        return PortRequestSerializer(ports[0]).data if ports else None


class ClaimRequestSerializer(serializers.Serializer):
    iccid = serializers.RegexField(r"^\d{18,22}F?$", required=False, allow_blank=True, default="")
    msisdn = serializers.CharField(max_length=24, required=False, allow_blank=True, default="")
    order_reference = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")


_CLAIM_MESSAGES = {
    LineClaim.Status.SUBMITTED: "Thanks — we'll check these details and get back to you.",
    LineClaim.Status.APPROVED: "This SIM has been added to your account.",
    LineClaim.Status.REJECTED: "We couldn't confirm this SIM belongs to you. Please contact support.",
}


class LineClaimSerializer(serializers.ModelSerializer):
    """Customer view of a claim — never reveals what (if anything) matched."""

    message = serializers.SerializerMethodField()
    line_id = serializers.SerializerMethodField()

    class Meta:
        model = LineClaim
        fields = ("id", "status", "message", "line_id", "created_at", "reviewed_at")

    def get_message(self, claim):
        return _CLAIM_MESSAGES[claim.status]

    def get_line_id(self, claim):
        return str(claim.resulting_line.uuid) if claim.resulting_line_id else None


class AddLineOrderSerializer(serializers.ModelSerializer):
    message = serializers.SerializerMethodField()
    line = serializers.SerializerMethodField()
    payment_error = serializers.SerializerMethodField()

    class Meta:
        model = AddLineOrder
        fields = (
            "order_reference", "payment_status", "fulfilment_status", "message",
            "sim_type", "switch_choice", "term", "quote", "total_gross", "vat_amount",
            "currency", "line", "created_at", "paid_at", "payment_error",
        )

    def get_message(self, order):
        return _order_message(order)

    def get_payment_error(self, order):
        """Latest decline while the order is still unpaid; None otherwise."""
        error = order.last_payment_error
        if not error or order.payment_status != AddLineOrder.PaymentStatus.PENDING:
            return None
        return {
            "code": error.get("decline_code") or error.get("code") or "payment_failed",
            "message": error.get("message") or "Your payment was declined.",
            "at": order.payment_failed_at,
        }

    def get_line(self, order):
        line: Line = order.line
        if not line:
            return None
        return {
            "id": str(line.uuid),
            "status": line.status,
            # Only shown once Transatel has allocated it.
            "msisdn": line.current_msisdn or None,
        }
