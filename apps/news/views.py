from rest_framework import generics, permissions
from rest_framework.pagination import PageNumberPagination

from .models import Article
from .serializers import ArticleDetailSerializer, ArticleListSerializer


class ArticlePagination(PageNumberPagination):
    page_size = 9
    page_size_query_param = "page_size"
    max_page_size = 50


class ArticleListView(generics.ListAPIView):
    """GET /api/news/  — published articles, newest first, paginated."""

    serializer_class = ArticleListSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = ArticlePagination

    def get_queryset(self):
        return Article.objects.filter(status=Article.Status.PUBLISHED)


class ArticleDetailView(generics.RetrieveAPIView):
    """GET /api/news/<slug>/  — a single published article with sections + quotes."""

    serializer_class = ArticleDetailSerializer
    permission_classes = [permissions.AllowAny]
    lookup_field = "slug"

    def get_queryset(self):
        return (
            Article.objects.filter(status=Article.Status.PUBLISHED)
            .prefetch_related("sections", "quotes")
        )
