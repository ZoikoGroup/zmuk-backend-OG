from rest_framework import serializers
from .models import BlogPost


class BlogPostListSerializer(serializers.ModelSerializer):
    """Used by the list endpoint.

    Deliberately excludes `content`. The listing page only shows a short
    summary, so shipping every post's full CKEditor HTML made the response
    far larger than it needed to be.
    """

    author = serializers.StringRelatedField()
    excerpt = serializers.CharField(read_only=True)

    class Meta:
        model = BlogPost
        fields = [
            'id', 'title', 'slug', 'author', 'featured_image',
            'excerpt', 'seo_description', 'created_at',
        ]


class BlogPostSerializer(serializers.ModelSerializer):
    """Used by the detail endpoint. Full post including content."""

    author = serializers.StringRelatedField()
    excerpt = serializers.CharField(read_only=True)

    class Meta:
        model = BlogPost
        fields = [
            'id', 'title', 'slug', 'author', 'content', 'featured_image',
            'excerpt', 'seo_title', 'seo_description', 'seo_keywords',
            'status', 'created_at', 'updated_at',
        ]