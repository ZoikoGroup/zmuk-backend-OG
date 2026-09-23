from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('blog', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='blogpost',
            name='notification_sent_at',
            field=models.DateTimeField(
                blank=True,
                editable=False,
                help_text='When the new-post email was sent to subscribers.',
                null=True,
            ),
        ),
        migrations.AlterModelOptions(
            name='blogpost',
            options={'ordering': ['-created_at']},
        ),
        migrations.AddIndex(
            model_name='blogpost',
            index=models.Index(
                fields=['status', '-created_at'],
                name='blog_status_created_idx',
            ),
        ),
    ]
