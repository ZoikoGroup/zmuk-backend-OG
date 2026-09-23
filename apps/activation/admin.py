from django.contrib import admin

from .models import SimActivation


@admin.register(SimActivation)
class SimActivationAdmin(admin.ModelAdmin):
    list_display = (
        "username",
        "email",
        "sim_serial",
        "zoiko_package",
        "created_at",
    )

    search_fields = (
        "username",
        "email",
        "sim_serial",
    )

    list_filter = (
        "zoiko_package",
        "created_at",
    )