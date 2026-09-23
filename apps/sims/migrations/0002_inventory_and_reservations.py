from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("sims", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="sim",
            name="inventory",
            field=models.CharField(
                choices=[
                    ("INSTOCK", "In stock"),
                    ("PENDING", "Pending (reserved in a cart)"),
                    ("RESERVED", "Reserved (assigned to an order)"),
                    ("ACTIVATED", "Activated"),
                ],
                db_index=True,
                default="INSTOCK",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="sim",
            name="order_reference",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="sim",
            name="activated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sim",
            name="activation_transaction_id",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.CreateModel(
            name="SimReservation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cart_key", models.CharField(db_index=True, max_length=100)),
                ("session_id", models.CharField(db_index=True, max_length=100)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("sim", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="reservations",
                    to="sims.sim",
                )),
            ],
            options={
                "verbose_name": "SIM reservation",
                "verbose_name_plural": "SIM reservations",
            },
        ),
        migrations.AddIndex(
            model_name="simreservation",
            index=models.Index(fields=["cart_key", "session_id"], name="sims_simres_cart_ke_d6756e_idx"),
        ),
    ]
