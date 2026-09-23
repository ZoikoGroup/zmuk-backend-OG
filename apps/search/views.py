from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django.db.models import Q
from django.utils.text import slugify


# correct model imports
from apps.plans.models import Plan
from apps.products.models import Product
from apps.blog.models import BlogPost
from apps.jobs.models import Job


@api_view(['GET'])
@permission_classes([AllowAny])
def global_search(request):

    key = request.GET.get("key")

    if not key:
        return Response({
            "status": False,
            "message": "Search key required",
            "data": []
        })

    results = []


    # ========================
    # PLAN SEARCH
    # ========================

    plans = Plan.objects.filter(
        Q(name__icontains=key)
    )[:10]

    for plan in plans:

        results.append({

            "type": "plan",

            "title": plan.name,

            "slug": plan.slug,

            "category": plan.category.name if hasattr(plan, "category") and plan.category else None,

            "category_slug": plan.category.slug if hasattr(plan, "category") and plan.category else None,

        })


    # ========================
    # PRODUCT SEARCH
    # ========================

    products = Product.objects.filter(
        Q(name__icontains=key)
    )[:10]

    for product in products:

        results.append({

            "type": "product",

            "title": product.name,

            "slug": product.slug,

            "category": product.category.name if hasattr(product, "category") and product.category else None,

            "category_slug": product.category.slug if hasattr(product, "category") and product.category else None,
            

        })


    # ========================
    # BLOG SEARCH
    # ========================

    blogs = BlogPost.objects.filter(
        Q(title__icontains=key)
    )[:10]

    for blog in blogs:

        results.append({

            "type": "blog",

            "title": blog.title,

            "slug": blog.slug,

            "category": None,

            "category_slug": None,

        })


    # ========================
    # JOB SEARCH
    # ========================

    jobs = Job.objects.filter(
        Q(title__icontains=key)
    )[:10]

    for job in jobs:

        results.append({

            "type": "job",

            "title": job.title,

            "slug": slugify(job.title),

            "category": None,

            "category_slug": None,

        })


    return Response({

        "status": True,

        "count": len(results),

        "data": results

    })