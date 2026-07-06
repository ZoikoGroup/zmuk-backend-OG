from django.contrib import admin
from django.urls import path, include
from django.http import HttpResponse
from django.conf import settings
from django.conf.urls.static import static

def home(request):
    return HttpResponse("DriverX Django API is running")

urlpatterns = [
    path('', home),
    path('admin/', admin.site.urls),
    path('ckeditor5/', include('django_ckeditor_5.urls')),
    path('api/blog/', include('apps.blog.api_urls', namespace='blog_api')),
    path('api/business-enterprise/', include('apps.business_enterprise.urls')),
    path('api/products/', include('apps.products.api_urls', namespace='products_api')),  # Keep this one
    path('api/plans/', include('apps.plans.api_urls')),
    path('api/accounts/', include('apps.accounts.urls')),
    # path("api/students/", include("apps.student_discount.urls")),
    # path("api/responder/", include("apps.first_responder.urls")),
    # path("api/military-discount/", include('apps.military_discount.urls')),
    # path("api/marine-discount/",include("apps.marine_discount.urls")),
    # path("api/senior-discount/", include("apps.senior_discount.urls")),
    
    path("api/",include("apps.travelpartners.urls")),
    path("api/v2/order/", include("apps.orders.urls")),
    path("api/v1/", include("apps.orders.urls")),
    path("api/",include("apps.activation.urls")),
path("api/", include("apps.contact.urls")),
    path("api/v1/", include("apps.coupons.api_urls")),
    path("api/", include("apps.switch.urls")),
   path("api/", include("apps.news.urls")),
    path('api/newsletter/', include('apps.newsletter.urls')),
    path('jobs/', include('apps.jobs.urls')),
    path('careers/', include('apps.careers.urls')),
    path('search/', include('apps.search.urls')),
    path('api/travel-ecosystem-partner/', include('apps.travel_ecosystem_partner.urls')),
    
    path("api/", include("apps.enterprise.urls")),
    path('api/', include('apps.integrations.urls')),
    path("api/security/", include("apps.security.urls")),
   path("api/support/", include("apps.support.urls")),
    


    # # path("api/products/", include("apps.products.urls")),  # Duplicate - commented out  # ← Remove/comment this duplicate line
    # path('api/v1/', include('apps.coupons.urls')),
    # path('api/form/', include('apps.forms_api.urls')),
    # path('api/form/', include('apps.demo_api.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
