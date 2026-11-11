from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("switch", "0003_backfill_switch_requests"),
    ]

    operations = [
        migrations.RemoveField(
            model_name='switchrequest',
            name='pac_code',
        ),
        migrations.RemoveField(
            model_name='switchrequest',
            name='stac_code',
        ),
        migrations.AlterField(
            model_name='switchrequest',
            name='reference',
            field=models.CharField(editable=False, max_length=20, unique=True),
        ),
    ]
