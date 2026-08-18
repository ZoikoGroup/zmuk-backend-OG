from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sims", "0003_sim_pending_contact"),
    ]

    operations = [
        migrations.CreateModel(
            name="Order",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order_reference", models.CharField(db_index=True, max_length=64, unique=True)),
                ("email", models.EmailField(blank=True, db_index=True, default="", max_length=254)),
                ("user_id", models.CharField(blank=True, default="", max_length=64)),
                ("country_code", models.CharField(blank=True, default="", max_length=8)),
                ("payload", models.JSONField(default=dict)),
                ("subscriber", models.JSONField(default=dict)),
                ("assigned", models.JSONField(default=list)),
                ("errors", models.JSONField(default=list)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("received", "Received"),
                            ("completed", "Completed"),
                            ("partial", "Partially completed"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="received",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
