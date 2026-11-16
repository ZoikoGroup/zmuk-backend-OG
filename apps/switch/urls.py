"""Mounted at /api/ in core/urls.py."""

from django.urls import path

from .views import (
    ContinuationCheckView,
    ContinuationClaimView,
    ContinuationResendView,
    ContractConsentView,
    ContractDocumentDownloadView,
    ContractDocumentsView,
    MySwitchRequestsView,
    OrderDocumentsView,
    OrderPreviewView,
    ReceiptDownloadView,
    SwitchFeedbackView,
    SwitchPortCodeView,
    SwitchQuoteView,
    SwitchRequestCreateView,
    SwitchRequestDetailView,
    SwitchRequestListView,
    SwitchCheckoutView,
    SwitchStatusByReferenceView,
)

app_name = "switch"

urlpatterns = [
    path("switch-requests/", SwitchRequestCreateView.as_view(), name="create"),
    path("switch-requests/mine/", MySwitchRequestsView.as_view(), name="mine"),
    # Milestone 4 (off until SWITCH_ORDER_CONVERSION_ENABLED)
    path("switch-requests/continue/check/", ContinuationCheckView.as_view(), name="continue-check"),
    path("switch-requests/continue/claim/", ContinuationClaimView.as_view(), name="continue-claim"),
    path("switch-requests/continue/resend/", ContinuationResendView.as_view(), name="continue-resend"),
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
    path("switch-requests/<str:reference>/order/", OrderPreviewView.as_view(), name="order-preview"),
    path("switch-requests/<str:reference>/documents/", ContractDocumentsView.as_view(), name="documents"),
    path("switch-requests/<str:reference>/documents/consent/", ContractConsentView.as_view(), name="consent"),
    path("switch-requests/<str:reference>/checkout/", SwitchCheckoutView.as_view(), name="checkout"),
    path("switch-requests/<str:reference>/order-documents/", OrderDocumentsView.as_view(), name="order-documents"),
    path(
        "switch-requests/<str:reference>/receipts/<uuid:receipt_id>/",
        ReceiptDownloadView.as_view(),
        name="receipt-download",
    ),
    path(
        "switch-requests/<str:reference>/documents/<uuid:document_id>/",
        ContractDocumentDownloadView.as_view(),
        name="document-download",
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
