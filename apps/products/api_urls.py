from django.urls import path
from .views import (
    ProductListView, ProductDetailView,
    RelatedProductsView, FeaturedProductsView,
    CategoryListView, VariantLookupView,
)

app_name = 'products_api'  # Add this line

urlpatterns = [
    path("", ProductListView.as_view(), name="product-list"),
    path("featured/", FeaturedProductsView.as_view(), name="product-featured"),
    path("categories/", CategoryListView.as_view(), name="category-list"),
    path("<slug:slug>/", ProductDetailView.as_view(), name="product-detail"),
    path("<slug:slug>/related/", RelatedProductsView.as_view(), name="product-related"),
    path("<slug:slug>/variant/", VariantLookupView.as_view(), name="product-variant-lookup"),
]
