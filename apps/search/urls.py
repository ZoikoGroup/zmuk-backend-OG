from django.urls import path
from .views import global_search

urlpatterns = [

    path('v1/api/search', global_search),

]