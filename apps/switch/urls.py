"""Mounted at /api/ in core/urls.py."""

from django.urls import path

from .views import (
    MySwitchRequestsView,
    SwitchFeedbackView,
    SwitchPortCodeView,
    SwitchQuoteView,
    SwitchRequestCreateView,
    SwitchRequestDetailView,
    SwitchRequestListView,
    SwitchStatusByReferenceView,
)

app_name = "switch"

urlpatterns = [
    path("switch-requests/", SwitchRequestCreateView.as_view(), name="create"),
    path("switch-requests/mine/", MySwitchRequestsView.as_view(), name="mine"),
    path(
        "switch-requests/by-reference/<str:reference>/",
        SwitchStatusByReferenceView.as_view(),
        name="by-reference",
    ),
    path(
        "switch-requests/<str:reference>/port-code/",
        SwitchPortCodeView.as_view(),
        name="port-code",
    ),
    path(
        "switch-requests/<str:reference>/feedback/",
        SwitchFeedbackView.as_view(),
        name="feedback",
    ),
    path("v1/switch/quote/", SwitchQuoteView.as_view(), name="quote"),
    # Staff only
    path("switch-requests/all/", SwitchRequestListView.as_view(), name="list"),
    path(
        "switch-requests/<int:pk>/",
        SwitchRequestDetailView.as_view(),
        name="detail",
    ),
]
