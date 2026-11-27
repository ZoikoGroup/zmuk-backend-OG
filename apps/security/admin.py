from django.contrib import admin
from .models import AccountSecurityEvent, UserSecurity, SuspiciousReport


@admin.register(UserSecurity)
class UserSecurityAdmin(admin.ModelAdmin):
    list_display = ("user", "two_fa_enabled", "updated_at")
    search_fields = ("user__username", "user__email")


@admin.register(SuspiciousReport)
class SuspiciousReportAdmin(admin.ModelAdmin):
    list_display = ("user", "created_at")
    search_fields = ("user__username", "user__email", "note")


@admin.register(AccountSecurityEvent)
class AccountSecurityEventAdmin(admin.ModelAdmin):
    """Read-only: recorded automatically, never edited by hand."""

    list_display = ("user", "kind", "occurred_at")
    list_filter = ("kind",)
    search_fields = ("user__username", "user__email")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
