from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sims", "0002_inventory_and_reservations"),
    ]

    operations = [
        migrations.AddField(
            model_name="sim",
            name="pending_contact",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
