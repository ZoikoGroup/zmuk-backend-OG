from django.urls import path
from .api_views import job_list_api

urlpatterns = [
     path('api/', job_list_api, name='job-list-api'),

]
