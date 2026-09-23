from django.contrib import admin
from .models import ContactMessage


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):

    list_display = (
        "name",
        "email",
        "phone",
        "subject",
        "created_at",
    )

    search_fields = (
        "name",
        "email",
        "phone",
    )

    list_filter = (
        "subject",
        "newsletter",
        "created_at",
    )