from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sims", "0005_sim_esim_qr"),
    ]

    operations = [
        migrations.AddField(
            model_name="sim",
            name="esim_qr_image",
            field=models.FileField(
                blank=True,
                help_text="The QR PNG decoded from esim_qr_data_url and saved to disk/media storage.",
                null=True,
                upload_to="esim_qr/%Y/%m/",
            ),
        ),
    ]
