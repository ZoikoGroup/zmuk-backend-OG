from django.contrib import admin

from .models import Article, ArticleQuote, ArticleSection


class ArticleSectionInline(admin.StackedInline):
    model = ArticleSection
    extra = 1
    fields = ("order", "heading", "body", "bullets")


class ArticleQuoteInline(admin.StackedInline):
    model = ArticleQuote
    extra = 1
    fields = ("order", "quote", "author", "role")


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "status", "published_at", "updated_at")
    list_filter = ("status", "category", "published_at")
    search_fields = ("title", "excerpt", "intro", "about", "source_name")
    list_editable = ("status",)
    prepopulated_fields = {"slug": ("title",)}
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-published_at", "-created_at")
    inlines = [ArticleSectionInline, ArticleQuoteInline]

    fieldsets = (
        ("Card / Header", {"fields": ("title", "slug", "category", "excerpt", "featured_image")}),
        ("Dateline", {"fields": ("location", "dateline_source")}),
        ("Body", {"fields": ("intro", "about")}),
        (
            "Media Contact",
            {"fields": ("contact_company", "contact_website", "contact_email", "contact_phone")},
        ),
        (
            "Source Information",
            {"fields": ("source_name", "source_city", "source_industry", "source_tag")},
        ),
        ("Publishing", {"fields": ("status", "published_at")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(ArticleSection)
class ArticleSectionAdmin(admin.ModelAdmin):
    list_display = ("article", "heading", "order")
    list_filter = ("article",)


@admin.register(ArticleQuote)
class ArticleQuoteAdmin(admin.ModelAdmin):
    list_display = ("article", "author", "role", "order")
    list_filter = ("article",)
