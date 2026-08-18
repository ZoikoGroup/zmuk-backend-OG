from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sims", "0004_order"),
    ]

    operations = [
        migrations.AddField(
            model_name="sim",
            name="esim_activation_code",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="sim",
            name="esim_matching_id",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="sim",
            name="esim_smdp_address",
            field=models.CharField(blank=True, default="", max_length=190),
        ),
        migrations.AddField(
            model_name="sim",
            name="esim_qr_value",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="sim",
            name="esim_qr_data_url",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="sim",
            name="esim_qr_status",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.AddField(
            model_name="sim",
            name="esim_qr_fetched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sim",
            name="esim_qr_emailed",
            field=models.BooleanField(default=False),
        ),
    ]
