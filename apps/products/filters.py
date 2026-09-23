import django_filters
from .models import Product


class ProductFilter(django_filters.FilterSet):
    min_price = django_filters.NumberFilter(
        field_name="variants__price", lookup_expr="gte", distinct=True
    )
    max_price = django_filters.NumberFilter(
        field_name="variants__price", lookup_expr="lte", distinct=True
    )
    brand = django_filters.CharFilter(field_name="brand", lookup_expr="icontains")
    category = django_filters.CharFilter(field_name="category__slug")

    # Dynamic attribute filtering: ?attr_storage=256gb or ?attr_color=black
    # Works for ANY attribute slug without code changes
    def __init__(self, data=None, *args, **kwargs):
        super().__init__(data, *args, **kwargs)
        if data:
            for key in data:
                if key.startswith("attr_"):
                    attr_slug = key[5:]  # strip "attr_"
                    self.filters[key] = django_filters.CharFilter(
                        method=self._make_attr_filter(attr_slug)
                    )

    @staticmethod
    def _make_attr_filter(attr_slug):
        def filter_fn(queryset, name, value):
            return queryset.filter(
                variants__variant_attributes__attribute_value__slug=value,
                variants__variant_attributes__attribute_value__attribute__slug=attr_slug,
            ).distinct()
        return filter_fn

    class Meta:
        model = Product
        fields = ["brand", "category", "min_price", "max_price"]
