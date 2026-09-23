from rest_framework import generics, filters, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from .models import Product, Category, ProductVariant, AttributeValue
from .serializers import (
    ProductListSerializer, ProductDetailSerializer,
    CategorySerializer, ProductVariantSerializer,
)
from .filters import ProductFilter


class CategoryListView(generics.ListAPIView):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer


class ProductListView(generics.ListAPIView):
    queryset = (
        Product.objects.filter(is_active=True)
        .prefetch_related("images", "variants__variant_attributes__attribute_value__attribute")
        .select_related("category")
    )
    serializer_class = ProductListSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ProductFilter
    search_fields = ["name", "brand", "model_number"]
    ordering_fields = ["created_at", "name"]
    ordering = ["-created_at"]


class ProductDetailView(generics.RetrieveAPIView):
    queryset = (
        Product.objects.filter(is_active=True)
        .prefetch_related(
            "images",
            "attributes__values",
            "variants__variant_attributes__attribute_value__attribute",
            "variants__image",
        )
        .select_related("category")
    )
    serializer_class = ProductDetailSerializer
    lookup_field = "slug"


class RelatedProductsView(APIView):
    def get(self, request, slug):
        try:
            product = Product.objects.get(slug=slug, is_active=True)
            related = (
                Product.objects.filter(category=product.category, is_active=True)
                .exclude(id=product.id)
                .prefetch_related("images")[:4]
            )
            serializer = ProductListSerializer(related, many=True, context={"request": request})
            return Response(serializer.data)
        except Product.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)


class VariantLookupView(APIView):
    """
    POST /api/products/<slug>/variant/
    Body: { "Storage": "256GB", "Color": "Black", "Condition": "Refurbished" }
    Returns the matching variant (price, stock, sku) or 404.

    Frontend calls this when user selects all attribute options.
    """
    def post(self, request, slug):
        try:
            product = Product.objects.get(slug=slug, is_active=True)
        except Product.DoesNotExist:
            return Response({"detail": "Product not found."}, status=status.HTTP_404_NOT_FOUND)

        selected = request.data  # { "Storage": "256GB", "Color": "Black" }

        # Find variant matching ALL selected attribute values
        variants = product.variants.filter(is_active=True)
        for attr_name, value in selected.items():
            variants = variants.filter(
                variant_attributes__attribute_value__attribute__name=attr_name,
                variant_attributes__attribute_value__value=value,
            )

        variant = variants.first()
        if not variant:
            return Response(
                {"detail": "No variant found for this combination."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ProductVariantSerializer(variant, context={"request": request})
        return Response(serializer.data)


class FeaturedProductsView(generics.ListAPIView):
    queryset = (
        Product.objects.filter(is_active=True, is_featured=True)
        .prefetch_related("images", "variants")
    )
    serializer_class = ProductListSerializer
