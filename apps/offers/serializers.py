from rest_framework import serializers

from .models import Offer


class OfferCardSerializer(serializers.ModelSerializer):
    """One card, in the shape the dashboard "Latest offers" page reads.

    The discount and code always come from the coupon, so the card can't
    disagree with what checkout charges. Information offers send them empty.
    """

    discount_type = serializers.SerializerMethodField()
    discount_value = serializers.SerializerMethodField()
    coupon_code = serializers.SerializerMethodField()
    combinable = serializers.SerializerMethodField()

    class Meta:
        model = Offer
        fields = [
            "id",
            "slug",
            "title",
            "subtitle",
            "badge",
            "mechanism",
            "bullets",
            "terms_text",
            "discount_type",
            "discount_value",
            "coupon_code",
            "starts_at",
            "ends_at",
            "combinable",
            "cta_label",
            "cta_href",
            "priority",
        ]

    def _coupon(self, offer):
        return offer.coupon if offer.mechanism == Offer.Mechanism.COUPON else None

    def get_discount_type(self, offer):
        coupon = self._coupon(offer)
        return coupon.type if coupon else None  # "percentage" or "flat"

    def get_discount_value(self, offer):
        coupon = self._coupon(offer)
        return f"{coupon.discount:.2f}" if coupon else None

    def get_coupon_code(self, offer):
        coupon = self._coupon(offer)
        return coupon.slug if coupon else ""

    def get_combinable(self, offer):
        # Checkout never combines a coupon with a public sector or student discount.
        return False
