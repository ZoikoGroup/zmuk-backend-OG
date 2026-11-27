# Hand-written: adds the RETIRED inventory choice (no database change beyond
# the choices list). The unrelated id-field drift in this app is left out.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sims", "0008_sim_inventory_purpose"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sim",
            name="inventory",
            field=models.CharField(
                choices=[
                    ("INSTOCK", "In stock"),
                    ("PENDING", "Pending (reserved in a cart)"),
                    ("RESERVED", "Reserved (assigned to an order)"),
                    ("ACTIVATED", "Activated"),
                    ("RETIRED", "Retired (replaced)"),
                ],
                db_index=True,
                default="INSTOCK",
                max_length=16,
            ),
        ),
    ]
