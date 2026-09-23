from rest_framework import serializers

from .models import Article, ArticleQuote, ArticleSection


class ArticleSectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ArticleSection
        fields = ["heading", "body", "bullets", "order"]


class ArticleQuoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ArticleQuote
        fields = ["quote", "author", "role", "order"]


class ArticleListSerializer(serializers.ModelSerializer):
    """Lightweight payload for the news list cards."""

    class Meta:
        model = Article
        fields = [
            "id",
            "title",
            "slug",
            "category",
            "excerpt",
            "featured_image",
            "location",
            "dateline_source",
            "published_at",
        ]


class ArticleDetailSerializer(serializers.ModelSerializer):
    """Full payload for a single article page, with nested sections + quotes."""

    sections = ArticleSectionSerializer(many=True, read_only=True)
    quotes = ArticleQuoteSerializer(many=True, read_only=True)

    class Meta:
        model = Article
        fields = [
            "id",
            "title",
            "slug",
            "category",
            "excerpt",
            "featured_image",
            # dateline
            "location",
            "dateline_source",
            "published_at",
            # body
            "intro",
            "sections",
            "quotes",
            "about",
            # media contact
            "contact_company",
            "contact_website",
            "contact_email",
            "contact_phone",
            # source information
            "source_name",
            "source_city",
            "source_industry",
            "source_tag",
            "updated_at",
        ]
