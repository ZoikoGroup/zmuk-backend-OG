"""Serializers for the SIM ordering API."""

from __future__ import annotations

from rest_framework import serializers

from .models import Order, Sim


class SimSerializer(serializers.ModelSerializer):
    sim_type = serializers.CharField(source="sim_type_key", read_only=True)

    class Meta:
        model = Sim
        fields = [
            "id", "iccid", "msisdn", "type_of_sim", "sim_type",
            "provisioning_status", "inventory", "order_reference",
            "activated_at", "activation_transaction_id",
        ]
        read_only_fields = fields


class LatestAvailableSimSerializer(serializers.ModelSerializer):
    """Payload for the availability lookup (read-only; no reservation).

    `type_of_sim` is the raw park-export value ("eUICC" / "UICC"); `sim_type`
    is the normalised "esim" / "psim" the storefront uses.
    """

    sim_type = serializers.CharField(source="sim_type_key", read_only=True)

    class Meta:
        model = Sim
        fields = [
            "iccid", "msisdn", "serial_number",
            "type_of_sim", "sim_type", "provisioning_status",
        ]
        read_only_fields = fields


class OrderItemSerializer(serializers.Serializer):
    """One checkout line — a SIM plan."""
    id = serializers.CharField(required=False, allow_blank=True)
    cartKey = serializers.CharField(required=False, allow_blank=True)
    name = serializers.CharField(required=False, allow_blank=True)
    quantity = serializers.IntegerField(min_value=1, default=1)
    simType = serializers.ChoiceField(choices=["esim", "psim"])
    transatelID = serializers.CharField()
    duration = serializers.IntegerField(required=False, allow_null=True)
    dataAllowance = serializers.CharField(required=False, allow_blank=True)


class SubscriberInfoSerializer(serializers.Serializer):
    """Contact details pushed to Transatel's /contact-info after activation.

    Normalised, snake_cased shape that maps 1:1 to the plugin's
    ``updateSubscriberContactN`` payload. Everything is optional so a partial
    address still activates; blanks are left to Transatel's own defaults.
    """
    title = serializers.CharField(required=False, allow_blank=True)
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name = serializers.CharField(required=False, allow_blank=True)
    date_of_birth = serializers.CharField(required=False, allow_blank=True)
    address = serializers.CharField(required=False, allow_blank=True)
    zip_code = serializers.CharField(required=False, allow_blank=True)
    city = serializers.CharField(required=False, allow_blank=True)
    country = serializers.CharField(required=False, allow_blank=True)  # ISO alpha-3, e.g. "GBR"
    point_of_sale = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)


class AddressSerializer(serializers.Serializer):
    """Raw checkout billing/shipping address (camelCase from the storefront).

    Only used as a fallback when the client doesn't send a normalised
    ``subscriber`` block — the view derives contact-info from it.
    """
    firstName = serializers.CharField(required=False, allow_blank=True)
    lastName = serializers.CharField(required=False, allow_blank=True)
    companyName = serializers.CharField(required=False, allow_blank=True)
    region = serializers.CharField(required=False, allow_blank=True)
    state = serializers.CharField(required=False, allow_blank=True)
    city = serializers.CharField(required=False, allow_blank=True)
    street = serializers.CharField(required=False, allow_blank=True)
    houseNumber = serializers.CharField(required=False, allow_blank=True)
    zip = serializers.CharField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)


class SimOrderSerializer(serializers.Serializer):
    """Payload the checkout posts to place a SIM order."""
    email = serializers.EmailField(required=False, allow_blank=True)
    user_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    order_reference = serializers.CharField(required=False, allow_blank=True)
    country_code = serializers.CharField(required=False, allow_blank=True)
    items = OrderItemSerializer(many=True)

    # Contact info for /contact-info. Send EITHER a normalised `subscriber`
    # block, OR the raw `billing` address and let the view normalise it.
    subscriber = SubscriberInfoSerializer(required=False)
    billing = AddressSerializer(required=False)


class ReserveSerializer(serializers.Serializer):
    simType = serializers.ChoiceField(choices=["esim", "psim"])
    quantity = serializers.IntegerField(min_value=1, default=1)
    cart_key = serializers.CharField()
    session_id = serializers.CharField()


class ReleaseSerializer(serializers.Serializer):
    cart_key = serializers.CharField()


class OrderSimSerializer(serializers.ModelSerializer):
    """One SIM within an order, for the order-detail / order-section view.

    eSIM QR fields are only ever populated for eSIMs (`Sim.is_esim`); pSIM
    lines always report `esim_qr_image_url: null` etc. — physical SIMs never
    get a QR generated.
    """

    sim_type = serializers.CharField(source="sim_type_key", read_only=True)
    esim_qr_image_url = serializers.SerializerMethodField()

    class Meta:
        model = Sim
        fields = [
            "iccid", "msisdn", "sim_type", "provisioning_status",
            "inventory", "activated_at", "activation_transaction_id",
            # eSIM QR / LPA — blank/null for pSIM lines.
            "esim_qr_image_url", "esim_qr_value", "esim_activation_code",
            "esim_smdp_address", "esim_qr_status", "esim_qr_emailed",
        ]
        read_only_fields = fields

    def get_esim_qr_image_url(self, sim: Sim) -> str | None:
        if not sim.is_esim or not sim.esim_qr_image:
            return None
        request = self.context.get("request")
        url = sim.esim_qr_image.url
        return request.build_absolute_uri(url) if request else url


class OrderDetailSerializer(serializers.ModelSerializer):
    """An order plus its assigned SIMs — what an 'order section' page needs."""

    sims = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "order_reference", "email", "billing_email", "user_id", "country_code",
            "status", "assigned", "errors", "created_at", "updated_at", "sims",
        ]
        read_only_fields = fields

    def get_sims(self, order: Order) -> list[dict]:
        """One entry per SIM actually activated for this order.

        Sourced from `order.assigned` — the snapshot written once, at the
        moment each SIM was successfully activated — NOT a live
        `Sim.objects.filter(order_reference=...)` lookup. `Sim.order_reference`
        is a plain mutable field: it's cleared back to "" if that SIM's
        activation failed and it was returned to stock, and it gets
        overwritten if the same ICCID is later reserved by a *different*
        order. Either way a live lookup can silently drop a SIM that really
        was part of this order (this is why a 2-SIM order could show only 1).
        `assigned` never changes after the order completes, so it's the
        reliable source for "how many SIMs did this order get".
        """
        assigned = order.assigned or []
        iccids = [a.get("iccid") for a in assigned if a.get("iccid")]
        sims_by_iccid = {s.iccid: s for s in Sim.objects.filter(iccid__in=iccids)}

        lines = []
        for a in assigned:
            iccid = a.get("iccid")
            sim = sims_by_iccid.get(iccid)
            if sim:
                # Live row still exists — use it for current status + QR.
                line = OrderSimSerializer(sim, context=self.context).data
            else:
                # Sim row missing (e.g. re-imported/deleted since) — fall
                # back to the order-time snapshot alone so the line still
                # appears, just without live status or a QR image.
                line = {
                    "iccid": iccid,
                    "msisdn": a.get("msisdn"),
                    "sim_type": a.get("sim_type"),
                    "provisioning_status": None,
                    "inventory": None,
                    "activated_at": None,
                    "activation_transaction_id": a.get("transaction_id", ""),
                    "esim_qr_image_url": None,
                    "esim_qr_value": "",
                    "esim_activation_code": "",
                    "esim_smdp_address": "",
                    "esim_qr_status": "",
                    "esim_qr_emailed": False,
                }
            line["transatelID"] = a.get("transatelID")
            line["transaction_id"] = a.get("transaction_id")
            lines.append(line)
        return lines