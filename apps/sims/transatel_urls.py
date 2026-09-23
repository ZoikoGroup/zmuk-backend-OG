"""URL routes for the Transatel live-lookup API.

Mounted in core/urls.py as:

    path("api/v1/transatel/", include("apps.sims.transatel_urls")),
"""

from django.urls import path

from . import transatel_views as views

app_name = "transatel_lookup"

urlpatterns = [
    # ── Phone → live Transatel data (the one you actually want) ──────────
    path("lookup-phone/<str:msisdn>/", views.LiveLookupByPhoneView.as_view(), name="lookup-phone"),

    # ── Direct ICCID lookups ─────────────────────────────────────────────
    path("subscriber/<str:iccid>/",    views.SubscriberBySerialView.as_view(), name="subscriber"),
    path("esim/<str:iccid>/",          views.EsimBySerialView.as_view(),       name="esim"),

    # ── Search ───────────────────────────────────────────────────────────
    path("search-sims/",               views.SearchSimsView.as_view(),         name="search-sims"),

    # ── Write operations (⚠️ real network effect) ────────────────────────
    path("reactivate/<str:iccid>/",    views.ReactivateSimView.as_view(),      name="reactivate"),
    path("suspend/<str:iccid>/",       views.SuspendSimView.as_view(),         name="suspend"),
    path("refresh-connectivity/<str:iccid>/", views.RefreshConnectivityView.as_view(), name="refresh-connectivity"),

    # ── Usage / CDR ──────────────────────────────────────────────────────
    path("usage/<str:iccid>/",         views.UsageCdrView.as_view(),           name="usage"),

    # ── SMS ──────────────────────────────────────────────────────────────
    path("sms/<str:iccid>/",           views.ReadSmsView.as_view(),            name="read-sms"),
    path("sms/<str:iccid>/send/",      views.SendSmsView.as_view(),            name="send-sms"),

    # ── Account ──────────────────────────────────────────────────────────
    path("account/",                   views.AccountInfoView.as_view(),        name="account"),

    # ── Webhooks ─────────────────────────────────────────────────────────
    path("webhooks/",                  views.ListWebhooksView.as_view(),       name="list-webhooks"),
    path("webhooks/create/",           views.CreateWebhookView.as_view(),      name="create-webhook"),
]