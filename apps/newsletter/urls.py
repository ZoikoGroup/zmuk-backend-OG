from django.urls import path

from .views import SubscribeView, UnsubscribeView

urlpatterns = [
    path('subscribe/', SubscribeView.as_view(), name='subscribe'),
    path('unsubscribe/<str:token>/', UnsubscribeView.as_view(), name='unsubscribe'),
]
