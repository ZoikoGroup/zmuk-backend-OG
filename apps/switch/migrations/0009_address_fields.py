from django.db import migrations, models


class Migration(migrations.Migration):
    """Step 3 of the 5-step journey: full address, optional contact number."""

    dependencies = [
        ("switch", "0008_backfill_action_owner"),
    ]

    operations = [
        migrations.AddField(
            model_name="switchrequest",
            name="address_line_1",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="switchrequest",
            name="address_line_2",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="switchrequest",
            name="town_city",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AlterField(
            model_name="switchrequest",
            name="mobile",
            field=models.CharField(blank=True, max_length=20),
        ),
    ]
