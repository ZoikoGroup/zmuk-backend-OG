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

    # --- Bookkeeping ---
    imported_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provisioning_status", "-first_activation_date", "iccid"]
        verbose_name = "SIM"
        verbose_name_plural = "SIMs"

    def __str__(self):
        return f"{self.iccid} ({self.msisdn or 'no MSISDN'})"
