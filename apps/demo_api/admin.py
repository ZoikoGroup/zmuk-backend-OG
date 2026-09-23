from django.contrib import admin
from .models import RequestDemo

@admin.register(RequestDemo)
class RequestDemoAdmin(admin.ModelAdmin):
    list_display = ('first_name', 'last_name', 'email', 'org_type', 'created_at')
    search_fields = ('first_name', 'last_name', 'email')
    list_filter = ('org_type', 'created_at')