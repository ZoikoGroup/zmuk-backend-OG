from django.contrib import admin
from django.utils.html import format_html

from .models import BlogPost


@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    list_display = ('thumb', 'title', 'author', 'status', 'created_at')
    list_display_links = ('title',)
    # Flip a post live without opening it.
    list_editable = ('status',)
    list_filter = ('status', 'created_at', 'author')
    search_fields = ('title', 'content', 'seo_title', 'seo_description')
    prepopulated_fields = {'slug': ('title',)}
    raw_id_fields = ('author',)
    date_hierarchy = 'created_at'
    # Was ('status', 'created_at') — that put drafts first and oldest first,
    # so the post you just wrote ended up at the bottom of the list.
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at')
    save_on_top = True

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