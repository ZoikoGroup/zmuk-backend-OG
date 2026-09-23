from django.contrib import admin
from .models import FirstResponderApplication

@admin.register(FirstResponderApplication)
class FirstResponderAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "email",
        "employment_status",
        "position",
        "created_at",
    )
