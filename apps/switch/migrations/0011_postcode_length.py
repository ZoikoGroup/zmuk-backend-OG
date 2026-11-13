from django.db import migrations, models


class Migration(migrations.Migration):
    """`postcode` now holds only a UK postcode (0010 moved the rest out)."""

    dependencies = [
        ("switch", "0010_split_legacy_postcode"),
    ]

    operations = [
        migrations.AlterField(
            model_name="switchrequest",
            name="postcode",
            field=models.CharField(blank=True, max_length=10),
        ),
    ]
