# Generated for CheckoutPayment (webhook source-of-truth for checkout)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0003_alter_bqorder_options'),
    ]

    operations = [
        migrations.CreateModel(
            name='CheckoutPayment',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order_ref', models.CharField(editable=False, max_length=24, unique=True)),
                ('stripe_payment_intent_id', models.CharField(blank=True, default='', max_length=255)),
                ('email', models.EmailField(blank=True, db_index=True, default='', max_length=254)),
                ('amount_pence', models.PositiveIntegerField()),
                ('currency', models.CharField(default='gbp', max_length=8)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('paid', 'Paid'), ('failed', 'Failed')], default='pending', max_length=20)),
                ('payload', models.JSONField()),
                ('processed', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
