from django.contrib import admin
from .models import SupportTicket, CallbackRequest


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ("id", "subject", "category", "status", "user", "created_at")
    list_filter = ("status", "category")
    search_fields = ("subject", "message", "user__username", "user__email")


@admin.register(CallbackRequest)
class CallbackRequestAdmin(admin.ModelAdmin):
    list_display = ("phone", "user", "handled", "created_at")
    list_filter = ("handled",)
    search_fields = ("phone", "user__username", "user__email")
