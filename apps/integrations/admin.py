from django.contrib import admin
from .models import IntegrationInterest

@admin.register(IntegrationInterest)
class IntegrationInterestAdmin(admin.ModelAdmin):
    list_display = (
        'full_name',
        'company_name',
        'business_email',
        'phone_number',
        'operating_regions',
        'traveler_volume',
        'created_at',
    )

    search_fields = (
        'full_name',
        'company_name',
        'business_email',
    )

    list_filter = (
        'operating_regions',
        'traveler_volume',
        'created_at',
    )

    readonly_fields = ('created_at',)