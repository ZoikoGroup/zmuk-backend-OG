"""CTO Milestone 04 (12 Nov 2026): request state machine, consent evidence,
quote snapshot, idempotency. Schema only; statuses are remapped in 0006."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('switch', '0004_drop_plain_codes_unique_reference'),
    ]

    operations = [
        # Renames keep existing values: an ops-entered date stays a confirmed
        # date, and a legacy completion time becomes the conversion time.
        migrations.RenameField(
            model_name='switchrequest',
            old_name='estimated_completion_date',
            new_name='confirmed_switch_date',
        ),
        migrations.RenameField(
            model_name='switchrequest',
            old_name='completed_at',
            new_name='converted_at',
        ),
        migrations.AlterField(
            model_name='switchrequest',
            name='converted_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='switchrequest',
            name='action_reason',
            field=models.CharField(blank=True, choices=[('code_needed', 'Switching code needed'), ('code_invalid', 'Switching code not accepted'), ('code_expired', 'Switching code expired'), ('eligibility', 'Eligibility check'), ('identity', 'Identity / ownership check'), ('duplicate', 'Possible duplicate request'), ('other', 'Other')], max_length=20),
        ),
        migrations.AddField(
            model_name='switchrequest',
            name='consent_ip',
            field=models.GenericIPAddressField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='switchrequest',
            name='consent_user_agent',
            field=models.CharField(blank=True, editable=False, max_length=255),
        ),
        migrations.AddField(
            model_name='switchrequest',
            name='consent_version',
            field=models.CharField(blank=True, editable=False, max_length=32),
        ),
        migrations.AddField(
            model_name='switchrequest',
            name='consented_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='switchrequest',
            name='existing_account_email',
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name='switchrequest',
            name='idempotency_key',
            field=models.CharField(blank=True, editable=False, max_length=64, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='switchrequest',
            name='ops_note',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='switchrequest',
            name='precontract_documents_sent_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='switchrequest',
            name='quote_hash',
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name='switchrequest',
            name='quote_snapshot',
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
        migrations.AddField(
            model_name='switchstatusevent',
            name='action_reason',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AlterField(
            model_name='switchrequest',
            name='status',
            field=models.CharField(choices=[('received', 'Request received'), ('validating', 'Checking your request'), ('action_needed', 'Action needed'), ('ready_for_order', 'Ready for your order'), ('converted', 'Order created'), ('cancelled', 'Cancelled'), ('expired', 'Expired'), ('rejected', 'Unable to proceed')], db_index=True, default='received', max_length=20),
        ),
    ]
