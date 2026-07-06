"""
Serializers for the Malima endpoints.

Two main payloads are validated here:

  AllocateSimsRequestSerializer
      Shape sent by /api/malima/allocate-sims (Next.js proxy):
          {
            "source": "...", "requested_at": "...",
            "cart": [
              {"cartIndex": 0, "unitIndex": 0, "planId": "...", "simType": "eSIM", "roamingZone": "Europe"},
              ...
            ]
          }

  MalimaOrderCreateSerializer
      Shape sent by /api/malima/save-order. The proxy wraps everything under `data`:
          {
            "source": "...", "captured_at": "...",
            "data": {
              "malima":  {"status": ..., "results": [...]},
              "allocations": [...],
              "enriched_cart": [...],
              "billingAddress": {...}, "shippingAddress": {...},
              "totals": {...}, "paymentMethod": "stripe", ...
            }
          }
"""

from rest_framework import serializers


# ── Allocate ─────────────────────────────────────────────────────────────────

class AllocateCartItemSerializer(serializers.Serializer):
    cartIndex = serializers.IntegerField(min_value=0)
    unitIndex = serializers.IntegerField(min_value=0)
    planId = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    transatelID, = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    simType = serializers.CharField()
    roamingZone = serializers.CharField(
        required=False, allow_null=True, allow_blank=True,
    )

    def validate_simType(self, value: str) -> str:
        v = (value or "").strip()
        if v.lower() not in ("esim", "psim"):
            raise serializers.ValidationError("simType must be 'eSIM' or 'pSIM'.")
        # Normalise capitalisation so allocation logic doesn't have to.
        return "eSIM" if v.lower() == "esim" else "pSIM"


class AllocateSimsRequestSerializer(serializers.Serializer):
    source = serializers.CharField(required=False, allow_blank=True, default="")
    requested_at = serializers.DateTimeField(required=False)
    cart = AllocateCartItemSerializer(many=True)

    def validate_cart(self, value: list) -> list:
        if not value:
            raise serializers.ValidationError("Cart must contain at least one item.")
        # Enforce uniqueness of (cartIndex, unitIndex) — the Next.js helper
        # relies on these as a join key.
        seen: set[tuple[int, int]] = set()
        for entry in value:
            k = (entry["cartIndex"], entry["unitIndex"])
            if k in seen:
                raise serializers.ValidationError(
                    f"Duplicate cart entry at cartIndex={k[0]}, unitIndex={k[1]}."
                )
            seen.add(k)
        return value


# ── Save order ───────────────────────────────────────────────────────────────

class MalimaOrderCreateSerializer(serializers.Serializer):
    """Top-level proxy envelope."""

    source = serializers.CharField(required=False, allow_blank=True, default="zoiko-orbit-checkout")
    captured_at = serializers.DateTimeField(required=False)
    data = serializers.DictField()

    def validate_data(self, value: dict) -> dict:
        if not isinstance(value, dict):
            raise serializers.ValidationError("`data` must be an object.")
        malima = value.get("malima")
        if not isinstance(malima, dict):
            raise serializers.ValidationError("`data.malima` is required.")
        if "results" not in malima or not isinstance(malima["results"], list):
            raise serializers.ValidationError(
                "`data.malima.results` must be a list (it may be empty)."
            )
        return value
