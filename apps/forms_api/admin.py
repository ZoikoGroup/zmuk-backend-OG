from django.contrib import admin
from .models import RequestForm

@admin.register(RequestForm)
class RequestFormAdmin(admin.ModelAdmin):
    list_display = ('first_name', 'last_name', 'email', 'request_type', 'created_at')
    list_filter = ('request_type', 'created_at')
    search_fields = ('first_name', 'last_name', 'email')