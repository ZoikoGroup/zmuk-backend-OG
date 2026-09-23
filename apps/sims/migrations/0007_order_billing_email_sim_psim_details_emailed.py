from django.db import migrations, models


def backfill_billing_email(apps, schema_editor):
    """Existing orders predate billing_email — seed it from `email` so
    fulfilment emails for in-flight orders keep working after this migration.
    """
    Order = apps.get_model("sims", "Order")
    Order.objects.filter(billing_email="").exclude(email="").update(billing_email=models.F("email"))


class Migration(migrations.Migration):

    dependencies = [
        ("sims", "0006_sim_esim_qr_image"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="billing_email",
            field=models.EmailField(blank=True, db_index=True, default="", max_length=254),
        ),
        migrations.AddField(
            model_name="sim",
            name="psim_details_emailed",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill_billing_email, migrations.RunPython.noop),
    ]
