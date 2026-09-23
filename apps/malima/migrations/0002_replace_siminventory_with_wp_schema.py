"""
Schema replacement for SimInventory.

The original SimInventory model used a simplified set of columns
(msisdn / iccid / imsi / sim_type / roaming_zone / batch / status). The
new schema mirrors the WordPress `orange-woo-order-api` plugin's
`wp_orange_sim_details` table exactly, with 62 carrier-CSV columns and
an `inventory` state column.

This migration:
  1. Deletes every existing SimReservation (FK to the table we're about
     to drop). They reference SIMs that are about to disappear.
  2. Drops the old SimInventory table.
  3. Recreates SimInventory with the new schema.
  4. Recreates the FK on SimReservation pointing at the new table.

DATA LOSS: every row currently in SimInventory is destroyed. Make a
backup of `apps_malima_siminventory` first if you haven't already.
"""

import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):

    dependencies = [
        ("malima", "0001_initial"),
    ]

    operations = [
        # ── 1. Wipe reservations (FK to the table we're replacing) ──────────
        migrations.RunSQL(
            sql="DELETE FROM malima_simreservation;",
            reverse_sql=migrations.RunSQL.noop,
            hints={"target_db": "default"},
        ),

        # ── 2. Drop the old SimInventory model (and its table) ──────────────
        migrations.RemoveField(model_name="simreservation", name="sim"),
        migrations.DeleteModel(name="SimInventory"),

        # ── 3. Recreate SimInventory with the new schema ────────────────────
        migrations.CreateModel(
            name="SimInventory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),

                ("simCardID", models.CharField(blank=True, default=None, max_length=50, null=True)),
                ("simIccid", models.CharField(db_index=True, max_length=25, unique=True)),
                ("msisdnData", models.CharField(blank=True, db_index=True, default=None, max_length=20, null=True, unique=True)),
                ("msisdnVoice", models.CharField(blank=True, db_index=True, default=None, max_length=20, null=True)),
                ("imsi", models.CharField(db_index=True, max_length=20, unique=True)),
                ("deviceImei", models.CharField(blank=True, default=None, max_length=20, null=True)),

                ("simStatus", models.CharField(blank=True, db_index=True, default=None, max_length=50, null=True)),
                ("subIdentifier", models.CharField(blank=True, default=None, max_length=50, null=True)),
                ("subscriberName", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("subCreationDate", models.DateTimeField(blank=True, null=True)),
                ("subOwner", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("customerOrderReference", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("simLastStatusRefreshDate", models.DateTimeField(blank=True, null=True)),
                ("simLastStatusChangeDate", models.DateTimeField(blank=True, null=True)),
                ("subIsConnectionDate", models.DateTimeField(blank=True, null=True)),
                ("simSuspensionReason", models.CharField(blank=True, default=None, max_length=100, null=True)),

                ("groupingCriteriaOne", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("groupListOne", models.CharField(blank=True, db_index=True, default=None, max_length=255, null=True)),
                ("groupingCriteriaTwo", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("groupListTwo", models.CharField(blank=True, default=None, max_length=255, null=True)),
                ("groupingCriteriaThree", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("groupListThree", models.CharField(blank=True, default=None, max_length=255, null=True)),

                ("deviceUniqueIdentifier", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("deviceSerialNumber", models.CharField(blank=True, db_index=True, default=None, max_length=100, null=True)),
                ("machineSerialNumber", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("machineName", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("deviceLocation", models.CharField(blank=True, default=None, max_length=255, null=True)),
                ("deviceCategory", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("deviceDescription", models.TextField(blank=True, default=None, null=True)),
                ("deviceAddress", models.CharField(blank=True, default=None, max_length=255, null=True)),
                ("deviceHolder", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("deviceCommunicationModuleBrand", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("deviceCommunicationModuleModel", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("deviceUpdateDate", models.DateTimeField(blank=True, null=True)),
                ("deviceContactName", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("deviceContactEmail", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("deviceContactPhone", models.CharField(blank=True, default=None, max_length=50, null=True)),
                ("machineDescription", models.TextField(blank=True, default=None, null=True)),
                ("machineUpdateDate", models.DateTimeField(blank=True, null=True)),

                ("simCardType", models.CharField(blank=True, default=None, max_length=50, null=True)),
                ("simProfile", models.CharField(blank=True, db_index=True, default=None, max_length=100, null=True)),

                ("networkConnectionStatus", models.CharField(blank=True, default=None, max_length=50, null=True)),
                ("networkConnectionLastUpdate", models.DateTimeField(blank=True, null=True)),
                ("networkConnectionCountry", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("networkConnectionOperator", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("networkConnectionRadioType", models.CharField(blank=True, default=None, max_length=50, null=True)),
                ("networkConnectionLastInteraction", models.DateTimeField(blank=True, null=True)),
                ("networkConnectionAPN", models.CharField(blank=True, default=None, max_length=100, null=True)),

                ("tariffPlanCode", models.CharField(blank=True, default=None, max_length=50, null=True)),
                ("tariffPlanLabel", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("activeOptionCodes", models.TextField(blank=True, default=None, null=True)),
                ("activeOptionLabels", models.TextField(blank=True, default=None, null=True)),

                ("eID", models.CharField(blank=True, default=None, max_length=100, null=True)),
                ("simProfileStatus", models.CharField(blank=True, default=None, max_length=50, null=True)),
                ("simCapacities", models.TextField(blank=True, default=None, null=True)),

                ("inventory", models.CharField(
                    choices=[
                        ("INSTOCK", "In stock (available for allocation)"),
                        ("PENDING", "Pending (reserved for an order)"),
                        ("OUTOFSTOCK", "Out of stock (consumed by an order)"),
                        ("RETIRED", "Retired (do not allocate)"),
                        ("TEST", "Test inventory (ACTIVATED_FOR_TEST)"),
                    ],
                    db_index=True, default="INSTOCK", max_length=20,
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "SIM inventory",
                "verbose_name_plural": "SIM inventory",
                "ordering": ("inventory", "id"),
            },
        ),

        # Indexes on (inventory, simProfile) and (inventory, groupListOne)
        # — same shape as the old model's hot-path index.
        migrations.AddIndex(
            model_name="siminventory",
            index=models.Index(
                fields=["inventory", "simProfile"],
                name="malima_simi_invent_5e8e0e_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="siminventory",
            index=models.Index(
                fields=["inventory", "groupListOne"],
                name="malima_simi_invent_g_idx",
            ),
        ),

        # ── 4. Restore the FK on SimReservation ─────────────────────────────
        migrations.AddField(
            model_name="simreservation",
            name="sim",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="reservations",
                to="malima.siminventory",
            ),
        ),
    ]
