from django.contrib import admin
from .models import UserSecurity, SuspiciousReport


@admin.register(UserSecurity)
class UserSecurityAdmin(admin.ModelAdmin):
    list_display = ("user", "two_fa_enabled", "updated_at")
    search_fields = ("user__username", "user__email")


@admin.register(SuspiciousReport)
class SuspiciousReportAdmin(admin.ModelAdmin):
    list_display = ("user", "created_at")
    search_fields = ("user__username", "user__email", "note")
