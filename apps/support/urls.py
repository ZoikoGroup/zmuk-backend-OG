from django.urls import path
from .views import TicketView, CallbackView

app_name = "support"

urlpatterns = [
    path("tickets/", TicketView.as_view(), name="tickets"),
    path("callback/", CallbackView.as_view(), name="callback"),
]
