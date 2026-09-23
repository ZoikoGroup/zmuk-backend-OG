from django.contrib import admin, messages
from django.utils.html import format_html

from .emails import notify_subscribers_of_post
from .models import BlogPost


@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    list_display = ('thumb', 'title', 'author', 'status', 'notified', 'created_at')
    list_display_links = ('title',)
    list_editable = ('status',)
    list_filter = ('status', 'created_at', 'author')
    search_fields = ('title', 'content', 'seo_title', 'seo_description')
    prepopulated_fields = {'slug': ('title',)}
    raw_id_fields = ('author',)
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at', 'notification_sent_at')
    save_on_top = True
    actions = ('resend_notification', 'clear_notification_flag')

    fieldsets = (
        ('Post', {
            'fields': ('title', 'slug', 'author', 'status', 'featured_image', 'content'),
        }),
        ('SEO', {
            'description': (
                'SEO description doubles as the summary shown on the /blogs '
                'cards. Leave it blank and the first ~160 characters of the '
                'content are used instead.'
            ),
            'fields': ('seo_title', 'seo_description', 'seo_keywords'),
        }),
        ('Subscriber notification', {
            'description': (
                'Subscribers are emailed automatically the first time a post is '
                'saved with status "Published". Saving again does not re-send. '
                'Use the "Re-send notification" action below if you need to.'
            ),
            'fields': ('notification_sent_at',),
        }),
        ('Timestamps', {
            'classes': ('collapse',),
            'fields': ('created_at', 'updated_at'),
        }),
    )

    @admin.display(description='Image')
    def thumb(self, obj):
        if not obj.featured_image:
            return '—'
        return format_html(
            '<img src="{}" style="height:38px;width:60px;object-fit:cover;border-radius:4px;">',
            obj.featured_image.url,
        )

    @admin.display(description='Emailed', boolean=True)
    def notified(self, obj):
        return obj.notification_sent_at is not None

    @admin.action(description="Re-send notification email to subscribers")
    def resend_notification(self, request, queryset):
        total = 0
        for post in queryset:
            if post.status != 'published':
                self.message_user(
                    request,
                    f"Skipped '{post.title}' — status is '{post.status}', not published.",
                    level=messages.WARNING,
                )
                continue
            sent = notify_subscribers_of_post(post, force=True)
            total += sent
            self.message_user(
                request,
                f"'{post.title}': {sent} email(s) sent.",
                level=messages.SUCCESS if sent else messages.ERROR,
            )
        if total == 0:
            self.message_user(
                request,
                "No emails were sent. Check that you have active subscribers and "
                "that the SMTP settings are correct.",
                level=messages.ERROR,
            )

    @admin.action(description="Clear 'emailed' flag (allows automatic re-send)")
    def clear_notification_flag(self, request, queryset):
        updated = queryset.update(notification_sent_at=None)
        self.message_user(
            request,
            f"Cleared the notification flag on {updated} post(s). "
            "They will be emailed again the next time they are saved as published.",
            level=messages.SUCCESS,
        )
