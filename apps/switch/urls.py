from django.urls import path

from .views import (
    SwitchRequestCreateView,
    SwitchRequestDetailView,
    SwitchRequestListView,
)

app_name = "switch"

urlpatterns = [
    path("switch-requests/", SwitchRequestCreateView.as_view(), name="create"),
    path("switch-requests/all/", SwitchRequestListView.as_view(), name="list"),
    path(
        "switch-requests/<int:pk>/",
        SwitchRequestDetailView.as_view(),
        name="detail",
    ),
]
