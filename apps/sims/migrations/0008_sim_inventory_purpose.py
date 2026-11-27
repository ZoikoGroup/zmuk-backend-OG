# Hand-written: only adds Sim.inventory_purpose. (makemigrations also wants to
# alter the id fields of this app — unrelated existing drift, left out on purpose.)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sims", "0007_order_billing_email_sim_psim_details_emailed"),
    ]

    operations = [
        migrations.AddField(
            model_name="sim",
            name="inventory_purpose",
            field=models.CharField(
                choices=[("sales", "Sales SIM"), ("swap", "Swap SIM")],
                db_index=True,
                default="sales",
                max_length=8,
            ),
        ),
    ]
