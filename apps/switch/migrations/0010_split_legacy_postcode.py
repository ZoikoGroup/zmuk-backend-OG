"""Old rows kept "Billing Address / Postcode" in one free-text `postcode`
field. Keep a valid postcode there (formatted "SW1A 1AA") and move the rest
of the text to address_line_1, so nothing is lost when 0011 shortens the
column. Text with no recognisable postcode goes to address_line_1 whole.
"""

import re

from django.db import migrations

_POSTCODE_AT_END = re.compile(r"(GIR\s*0AA|[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2})\W*$", re.IGNORECASE)


def _format(postcode):
    compact = re.sub(r"\s+", "", postcode).upper()
    return f"{compact[:-3]} {compact[-3:]}"


def forwards(apps, schema_editor):
    SwitchRequest = apps.get_model("switch", "SwitchRequest")
    for row in SwitchRequest.objects.exclude(postcode="").only("pk", "postcode", "address_line_1"):
        text = row.postcode.strip()
        match = _POSTCODE_AT_END.search(text)
        if match:
            postcode = _format(match.group(1))
            rest = text[:match.start()].strip(" ,\n\t")
        else:
            postcode, rest = "", text
        address = row.address_line_1 or rest[:255]
        SwitchRequest.objects.filter(pk=row.pk).update(postcode=postcode, address_line_1=address)


def backwards(apps, schema_editor):
    SwitchRequest = apps.get_model("switch", "SwitchRequest")
    for row in SwitchRequest.objects.exclude(address_line_1="").only("pk", "postcode", "address_line_1"):
        joined = ", ".join(p for p in (row.address_line_1, row.postcode) if p)
        SwitchRequest.objects.filter(pk=row.pk).update(postcode=joined[:255])


class Migration(migrations.Migration):

    dependencies = [
        ("switch", "0009_address_fields"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
