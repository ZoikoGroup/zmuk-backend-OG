"""Routes mounted under /api/v1/ in core/urls.py (Change SIM type, CT-005)."""

from django.urls import path

from . import views

app_name = "sim_change"

urlpatterns = [
    path("lines/<uuid:line_id>/sim-type-options/", views.SimTypeOptionsView.as_view(), name="options"),
    path("lines/<uuid:line_id>/sim-type-changes/quote/", views.SimTypeQuoteView.as_view(), name="quote"),
    path("lines/<uuid:line_id>/sim-type-changes/", views.SimTypeChangeCreateView.as_view(), name="create"),
    path("sim-type-changes/", views.SimTypeChangeListView.as_view(), name="list"),
    path("sim-type-changes/report-unauthorised-link/", views.ReportFromLinkView.as_view(), name="report-link"),
    path("sim-type-changes/<uuid:case_id>/", views.SimTypeChangeDetailView.as_view(), name="detail"),
    path("sim-type-changes/<uuid:case_id>/confirm/", views.SimTypeChangeConfirmView.as_view(), name="confirm"),
    path("sim-type-changes/<uuid:case_id>/cancel/", views.SimTypeChangeCancelView.as_view(), name="cancel"),
    path("sim-type-changes/<uuid:case_id>/confirm-receipt/", views.SimTypeChangeReceiptView.as_view(),
         name="confirm-receipt"),
    path("sim-type-changes/<uuid:case_id>/report-unauthorised/", views.SimTypeChangeReportView.as_view(),
         name="report-unauthorised"),
    path("sim-type-changes/<uuid:case_id>/activation-access/", views.SimTypeChangeEsimAccessView.as_view(),
         name="activation-access"),
    path("sim-type-changes/<uuid:case_id>/installed/", views.SimTypeChangeInstalledView.as_view(), name="installed"),
]
