"""Fill action_owner on existing "action needed" requests and their history.

Code reasons belong to the customer; everything else to Operations, the same
mapping as SwitchRequest.DEFAULT_ACTION_OWNER (copied here so later model
changes can't alter what this migration did).
"""

from django.db import migrations

CUSTOMER_REASONS = ("code_needed", "code_invalid", "code_expired")


def forwards(apps, schema_editor):
    for name in ("SwitchRequest", "SwitchStatusEvent"):
        model = apps.get_model("switch", name)
        field = "status" if name == "SwitchRequest" else "status_after"
        needed = model.objects.filter(**{field: "action_needed"}, action_owner="")
        needed.filter(action_reason__in=CUSTOMER_REASONS).update(action_owner="customer")
        needed.exclude(action_reason__in=CUSTOMER_REASONS).update(action_owner="operations")


def backwards(apps, schema_editor):
    pass  # The field is dropped by reversing 0007.


class Migration(migrations.Migration):

    dependencies = [
        ("switch", "0007_action_owner"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
