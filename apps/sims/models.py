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
    # Customer contact block awaiting push to Transatel's /contact-info. Set when
    # activation is submitted but the SIM is still AVAILABLE (async provisioning),
    # since /contact-info is rejected until the SIM is Active. The
    # `push_pending_contact` management command drains this once the SIM goes live.
    pending_contact = models.JSONField(null=True, blank=True, default=None)

    # ── eSIM QR / LPA profile (populated after activation, eSIMs only) ──
    # Fetched from Transatel's esims endpoint and emailed to the customer. The
    # profile isn't ready the instant we activate (async), so these stay blank
    # until `send_esim_qr` (or the best-effort inline call) fills them in.
    esim_activation_code = models.TextField(blank=True, default="")   # full LPA activation code
    esim_matching_id = models.CharField(max_length=120, blank=True, default="")
    esim_smdp_address = models.CharField(max_length=190, blank=True, default="")
    esim_qr_value = models.TextField(blank=True, default="")          # LPA:1$smdp$matchingId
    esim_qr_data_url = models.TextField(blank=True, default="")       # data:image/png;base64,...
    esim_qr_image = models.FileField(                                  # the PNG itself, saved to MEDIA
        upload_to="esim_qr/%Y/%m/", blank=True, null=True,
        help_text="The QR PNG decoded from esim_qr_data_url and saved to disk/media storage.",
    )
    esim_qr_status = models.CharField(max_length=40, blank=True, default="")
    esim_qr_fetched_at = models.DateTimeField(null=True, blank=True)
    esim_qr_emailed = models.BooleanField(default=False)

    # ── pSIM details email (physical SIMs only) ──
    # ICCID / IMSI / MSISDN emailed to the order's billing_email once the SIM
    # is activated. Mirrors esim_qr_emailed's idempotency guard so a SIM's
    # details are only ever sent once.
    psim_details_emailed = models.BooleanField(default=False)

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
        """True when this is an eSIM.

        The park export's `type_of_sim` discriminator is "eUICC" for embedded
        (eSIM) and "UICC" for physical. Note "UICC" is a substring of "eUICC",
        so we must test for "euicc"/"esim" positively rather than for "uicc".
        Kept in lockstep with `reservations._available_queryset`.
        """
        t = (self.type_of_sim or "").strip().lower()
        return "euicc" in t or "esim" in t

    @property
    def sim_type_key(self) -> str:
        """Normalised "esim" | "psim" for API/reservation matching."""
        return "esim" if self.is_esim else "psim"


class Order(models.Model):
    """The Django-side record of a placed SIM order.

    Created by `SimOrderView.post` (POST /api/v1/sims/sim-orders/, proxied by
    the storefront's /api/transatel/sim-orders route) so every checkout is
    durably logged, independent of the `Sim.order_reference` stamps that track
    which SIM belongs to which order. `payload` keeps the full request body
    (cart lines with pricing, etc.) for audit; `subscriber` is the normalised
    contact block actually sent to Transatel's /contact-info (built from the
    billing address — see `transatel.service.build_subscriber_info`).
    """

    STATUS_RECEIVED = "received"
    STATUS_COMPLETED = "completed"
    STATUS_PARTIAL = "partial"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_RECEIVED, "Received"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_PARTIAL, "Partially completed"),
        (STATUS_FAILED, "Failed"),
    ]

    order_reference = models.CharField(max_length=64, unique=True, db_index=True)
    email = models.EmailField(blank=True, default="", db_index=True)
    # Recipient for fulfilment emails (eSIM QR / pSIM ICCID-IMSI-MSISDN details).
    # Resolved from the checkout's billing address email, falling back to
    # `email` above when the billing address didn't include one — see
    # `SimOrderView.post`. Deliberately a separate field from `email` since a
    # customer's account email and their billing contact can differ.
    billing_email = models.EmailField(blank=True, default="", db_index=True)
    user_id = models.CharField(max_length=64, blank=True, default="")
    country_code = models.CharField(max_length=8, blank=True, default="")

    payload = models.JSONField(default=dict)      # full request body, as posted
    subscriber = models.JSONField(default=dict)    # normalised billing/contact block sent to Transatel

    assigned = models.JSONField(default=list)       # SIMs assigned + activation results
    errors = models.JSONField(default=list)         # soft errors / partial failures

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RECEIVED, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.order_reference} · {self.email or 'no email'} · {self.get_status_display()}"


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