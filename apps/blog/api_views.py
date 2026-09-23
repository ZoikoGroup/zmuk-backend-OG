from rest_framework import generics
from rest_framework.exceptions import NotFound

from .models import BlogPost
from .serializers import BlogPostSerializer, BlogPostListSerializer


PUBLISHED = BlogPost.objects.filter(status='published').select_related('author')


class BlogPostListAPI(generics.ListAPIView):
    """GET /api/blog/posts/

    Paginated (settings.py -> PAGE_SIZE: 9), so the response is
    {count, next, previous, results: [...]} — not a bare list.

    Supports ?search=<term> and ?ordering=created_at / -created_at.
    `search_fields` was missing before, so SearchFilter silently did nothing.
    """

    serializer_class = BlogPostListSerializer
    search_fields = ['title', 'seo_description', 'seo_keywords', 'content']
    ordering_fields = ['created_at', 'title']
    ordering = ['-created_at']

    def get_queryset(self):
        return PUBLISHED.all()


class BlogPostDetailAPI(generics.RetrieveAPIView):
    """GET /api/blog/posts/<slug>/"""

    serializer_class = BlogPostSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return PUBLISHED.all()


class BlogPostRelatedAPI(generics.ListAPIView):
    """GET /api/blog/posts/<slug>/related/

    Up to 3 other published posts, newest first. Unpaginated so the sidebar
    gets a plain list back instead of a {results: [...]} wrapper.
    """

    serializer_class = BlogPostListSerializer
    pagination_class = None
    filter_backends = []

    def get_queryset(self):
        slug = self.kwargs['slug']
        if not PUBLISHED.filter(slug=slug).exists():
            raise NotFound('Post not found.')
        return PUBLISHED.exclude(slug=slug)[:3]