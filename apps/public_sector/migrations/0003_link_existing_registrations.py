"""Link registrations sent before login was required to their accounts (by email)."""

from django.conf import settings
from django.db import migrations

from apps.plans.eligibility import link_applications_by_email


def link_registrations(apps, schema_editor):
    Registration = apps.get_model("public_sector", "PublicSectorRegistration")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    link_applications_by_email(Registration, User, ("pending", "approved"))


class Migration(migrations.Migration):

    dependencies = [
        ("public_sector", "0002_user_and_review_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(link_registrations, migrations.RunPython.noop),
    ]
