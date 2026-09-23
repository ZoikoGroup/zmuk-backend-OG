from django.urls import path
from . import api_views

app_name = 'blog_api'

urlpatterns = [
    path('posts/', api_views.BlogPostListAPI.as_view(), name='blog_list_api'),
    # More specific route first — otherwise <slug> would swallow "related".
    path('posts/<slug:slug>/related/', api_views.BlogPostRelatedAPI.as_view(), name='blog_related_api'),
    path('posts/<slug:slug>/', api_views.BlogPostDetailAPI.as_view(), name='blog_detail_api'),
]