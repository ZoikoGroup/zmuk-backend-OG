"""Prepare existing student applications for the verification flow.

1. Approved applications whose graduation date has passed become "expired".
2. Applications sent before login was required are linked to their accounts (by email).
3. Uploaded student ID documents move from public media to private storage.
"""

import logging

from django.conf import settings
from django.db import migrations
from django.utils import timezone

from apps.plans.eligibility import link_applications_by_email

logger = logging.getLogger(__name__)


def expire_and_link(apps, schema_editor):
    Application = apps.get_model("student_discount", "StudentDiscountApplication")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))

    Application.objects.filter(
        status="approved", graduation_date__lt=timezone.localdate()
    ).update(status="expired")

    link_applications_by_email(Application, User, ("pending", "under_review", "approved"))


def move_documents(apps, schema_editor):
    from django.core.files.storage import default_storage

    from apps.student_discount.storage import private_document_storage

    Application = apps.get_model("student_discount", "StudentDiscountApplication")
    private = private_document_storage()

    for pk, name in Application.objects.exclude(student_id_document="").values_list(
        "pk", "student_id_document"
    ):
        if private.exists(name):
            continue
        try:
            with default_storage.open(name) as public_file:
                new_name = private.save(name, public_file)
        except Exception:
            # Missing or unreadable file: leave the row alone, the admin shows it as unavailable.
            logger.warning("Student document %s (application %s) could not be moved", name, pk)
            continue
        Application.objects.filter(pk=pk).update(student_id_document=new_name)
        try:
            default_storage.delete(name)
        except Exception:
            logger.warning("Old public copy of %s could not be deleted", name)


class Migration(migrations.Migration):

    dependencies = [
        ("student_discount", "0002_user_review_fields_private_document"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(expire_and_link, migrations.RunPython.noop),
        migrations.RunPython(move_documents, migrations.RunPython.noop),
    ]
