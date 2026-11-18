"""Public sector workers now get a % off normal plans, not their own plans.

Any plan already saved with the old "public_sector" audience becomes a normal
plan, but switched OFF (is_active=False), so a cheaper copy of a normal plan
doesn't suddenly appear on the public pages. Delete or reuse it in the admin.
"""

from django.db import migrations, models


def retire_public_sector_plans(apps, schema_editor):
    Plan = apps.get_model("plans", "Plan")
    Plan.objects.filter(audience="public_sector").update(audience="general", is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ('plans', '0006_plan_audience'),
    ]

    operations = [
        migrations.RunPython(retire_public_sector_plans, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='plan',
            name='audience',
            field=models.CharField(choices=[('general', 'Everyone'), ('student', 'Students (verified only)')], db_index=True, default='general', help_text='Student plans are only listed on the student page and can only be bought by approved students. Public sector discounts are set in Public Sector › Public sector offer, on normal plans.', max_length=20),
        ),
    ]
