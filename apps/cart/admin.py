from django.contrib import admin

from .models import Cart, CartLine


class CartLineInline(admin.TabularInline):
    model = CartLine
    extra = 0


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "session_key", "updated_at")
    inlines = [CartLineInline]
