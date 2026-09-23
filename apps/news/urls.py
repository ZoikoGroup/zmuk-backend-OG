from django.urls import path

from .views import ArticleDetailView, ArticleListView

app_name = "news"

urlpatterns = [
    path("news/", ArticleListView.as_view(), name="list"),
    path("news/<slug:slug>/", ArticleDetailView.as_view(), name="detail"),
]
