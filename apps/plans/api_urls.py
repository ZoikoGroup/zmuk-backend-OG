from django.urls import path
from .api_views import (
    PlanListAPI,
    PlanDetailAPI,
    PlanByCategoryAPI,
)

urlpatterns = [
    path("v1/", PlanListAPI.as_view()),
    path("v1/<slug:slug>/", PlanDetailAPI.as_view()),
    path("v1/category/<slug:slug>/", PlanByCategoryAPI.as_view()),
]
