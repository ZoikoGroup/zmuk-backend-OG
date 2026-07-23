from django.db import models


class Sim(models.Model):
    """One row of the Transatel SIM park export.

    ICCID is treated as the natural unique key so re-importing the same
    export updates existing rows instead of creating duplicates.
    """

    # --- Identity ---
    serial_number = models.CharField(max_length=64, blank=True, default="")
    iccid = models.CharField("ICCID", max_length=32, unique=True, db_index=True)
    imsi = models.CharField("IMSI", max_length=32, blank=True, default="")
    msisdn = models.CharField("MSISDN", max_length=24, blank=True, default="")
    type_of_sim = models.CharField(max_length=64, blank=True, default="")
    sim_reference = models.CharField(max_length=64, blank=True, default="")

    # --- Status ---
    provisioning_status = models.CharField(max_length=32, blank=True, default="", db_index=True)
    prepaid_status = models.CharField(max_length=32, blank=True, default="", db_index=True)

    # --- Owner / account ---
    subscriber = models.CharField(max_length=128, blank=True, default="")
    company = models.CharField(max_length=128, blank=True, default="")
    reference = models.CharField(max_length=64, blank=True, default="")
    customer_account = models.CharField(max_length=128, blank=True, default="")
    group = models.CharField(max_length=128, blank=True, default="")
    point_of_sale = models.CharField(max_length=64, blank=True, default="")

    # --- Dates ---
    first_activation_date = models.DateTimeField(null=True, blank=True)
    last_action_date = models.DateTimeField(null=True, blank=True)
    last_seen_date = models.DateTimeField(null=True, blank=True)

    # --- Service ---
    provisioning_action_number = models.CharField(max_length=64, blank=True, default="")
    subscriber_number = models.CharField(max_length=64, blank=True, default="")
    service_pack = models.CharField(max_length=128, blank=True, default="")
    service_profile = models.CharField(max_length=128, blank=True, default="")
    rate_plan = models.CharField(max_length=128, blank=True, default="")

    # --- Network / device ---
    last_imei = models.CharField("Last IMEI", max_length=32, blank=True, default="")
    last_origin_country = models.CharField(max_length=64, blank=True, default="")
    last_mcc = models.CharField("Last MCC", max_length=8, blank=True, default="")
    last_mnc = models.CharField("Last MNC", max_length=8, blank=True, default="")
    last_rat = models.CharField("Last RAT", max_length=8, blank=True, default="")
    last_destination = models.CharField(max_length=64, blank=True, default="")

    # --- Usage ---
    data_usage_gb = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    voice_usage = models.CharField(max_length=32, blank=True, default="")
    sms_usage = models.CharField(max_length=32, blank=True, default="")
    mms_usage = models.CharField(max_length=32, blank=True, default="")

    # --- Inventory / fulfilment ---------------------------------------------
    # Mirrors the WordPress plugin's `inventory` column + `order_id`. The park
    # export's `provisioning_status` describes the SIM at the operator; this
    # tracks where the SIM sits in *our* sell → reserve → activate pipeline.
    class Inventory(models.TextChoices):
        IN_STOCK = "INSTOCK", "In stock"
        PENDING = "PENDING", "Pending (reserved in a cart)"
        RESERVED = "RESERVED", "Reserved (assigned to an order)"
        ACTIVATED = "ACTIVATED", "Activated"

    inventory = models.CharField(
        max_length=16,
        choices=Inventory.choices,
        default=Inventory.IN_STOCK,
        db_index=True,
    )
    # Order this SIM was assigned to (free-form reference, e.g. "WC-1042").
    order_reference = models.CharField(max_length=64, blank=True, default="", db_index=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    activation_transaction_id = models.CharField(max_length=128, blank=True, default="")

    # --- Bookkeeping ---
    imported_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provisioning_status", "-first_activation_date", "iccid"]
        verbose_name = "SIM"
        verbose_name_plural = "SIMs"

    def __str__(self):
        return f"{self.iccid} ({self.msisdn or 'no MSISDN'})"

    # ── Helpers ─────────────────────────────────────────────────────────────

    @property
    def is_esim(self) -> bool:
        """True when this is an eSIM, matched on `type_of_sim` (the discriminator)."""
        return "esim" in (self.type_of_sim or "").strip().lower()

    @property
    def sim_type_key(self) -> str:
        """Normalised "esim" | "psim" for API/reservation matching."""
        return "esim" if self.is_esim else "psim"


class SimReservation(models.Model):
    """Short-lived hold placed on a SIM while it sits in a cart.

    Port of the plugin's `wp_transatel_sim_reservations` table. A background
    job (see `manage.py cleanup_reservations`) releases holds whose
    `expires_at` has passed, returning the SIM to stock.
    """

    sim = models.ForeignKey(
        Sim, on_delete=models.CASCADE, related_name="reservations", db_index=True
    )
    cart_key = models.CharField(max_length=100, db_index=True)
    session_id = models.CharField(max_length=100, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "SIM reservation"
        verbose_name_plural = "SIM reservations"
        indexes = [
            models.Index(fields=["cart_key", "session_id"]),
        ]

    def __str__(self):
        return f"hold {self.sim_id} · {self.cart_key} · until {self.expires_at:%Y-%m-%d %H:%M}"
