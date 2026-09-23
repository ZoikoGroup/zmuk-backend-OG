from rest_framework import generics, filters
from rest_framework.viewsets import ReadOnlyModelViewSet
from django_filters.rest_framework import DjangoFilterBackend

from .models import Product, ProductCategory
from .serializers import ProductSerializer, ProductCategorySerializer 
from .filters import ProductFilter


# -----------------------------
# Product List API (With Filters)
# -----------------------------
class ProductListAPIView(generics.ListAPIView):
    queryset = (
        Product.objects
        .select_related('category')
        .prefetch_related(
            'attributes',
            'images',
            'variants',
            'variants__images'
        )
        .distinct()
        .all()
    )
    serializer_class = ProductSerializer

    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]

    filterset_class = ProductFilter
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'variants__regular_price']
    ordering = ['-created_at']


# -----------------------------
# Product Detail API
# -----------------------------
class ProductDetailAPIView(generics.RetrieveAPIView):
    queryset = (
        Product.objects
        .select_related('category')
        .prefetch_related(
            'attributes',
            'images',
            'variants',
            'variants__images'
        )
        .all()
    )
    serializer_class = ProductSerializer
    lookup_field = 'slug'


# -----------------------------
# Category List API
# -----------------------------
class CategoryListAPIView(generics.ListAPIView):
    queryset = ProductCategory.objects.filter(is_active=True)
    serializer_class = ProductCategorySerializer


# -----------------------------
# Optional ViewSet Version
# -----------------------------
class ProductViewSet(ReadOnlyModelViewSet):
    queryset = (
        Product.objects
        .select_related('category')
        .prefetch_related(
            'attributes',
            'images',
            'variants',
            'variants__images'
        )
        .distinct()
        .all()
    )
    serializer_class = ProductSerializer

    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]

    filterset_class = ProductFilter
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'variants__regular_price']
    ordering = ['-created_at']