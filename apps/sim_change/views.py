"""Customer API for Change SIM type (CTO v2.0 §8, Addendum v2.1).

    GET  /api/v1/lines/<line_id>/sim-type-options/
    POST /api/v1/lines/<line_id>/sim-type-changes/quote/      {target_type, device}
    POST /api/v1/lines/<line_id>/sim-type-changes/            Idempotency-Key; {target_type, device, quote, consent, delivery_address?}
    GET  /api/v1/sim-type-changes/
    GET  /api/v1/sim-type-changes/<case_id>/
    POST /api/v1/sim-type-changes/<case_id>/confirm/          {quote}
    POST /api/v1/sim-type-changes/<case_id>/cancel/           {expected_version?}
    POST /api/v1/sim-type-changes/<case_id>/confirm-receipt/  {iccid}
    POST /api/v1/sim-type-changes/<case_id>/report-unauthorised/ {note?}

Every route needs a logged-in customer and only sees their own lines and
cases (404 otherwise, never revealing that another customer's exists).
Responses are `Cache-Control: no-store`. No customer request changes
anything at the supplier.
"""

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from apps.add_line.models import Line

from .models import SimTypeChangeCase
from .services import eligibility, idempotency, presenter, quote, requests
from .services.address import AddressError
from .services.transitions import TransitionError


def _error(exc: TransitionError) -> Response:
    body = {"code": exc.code, "message": str(exc)}
    if isinstance(exc, AddressError):
        body["errors"] = exc.errors
    if getattr(exc, "correlation_id", ""):
        body["correlation_id"] = exc.correlation_id
    return _no_store(Response(body, status=exc.http_status))


def _no_store(response: Response) -> Response:
    response["Cache-Control"] = "no-store"
    return response


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")) or None


class _CustomerView(APIView):
    permission_classes = [IsAuthenticated]

    def own_line(self, request, line_id) -> Line:
        return get_object_or_404(Line.objects.select_related("sim", "plan"), uuid=line_id, owner=request.user)

    def own_case(self, request, case_id) -> SimTypeChangeCase:
        return get_object_or_404(
            SimTypeChangeCase.objects.select_related("line", "new_sim"), uuid=case_id, owner=request.user,
        )


class SimTypeOptionsView(_CustomerView):
    def get(self, request, line_id):
        result = eligibility.assess(request.user, self.own_line(request, line_id))
        code = status.HTTP_503_SERVICE_UNAVAILABLE if result.supplier_unavailable else status.HTTP_200_OK
        return _no_store(Response(presenter.options_view(result), status=code))


class SimTypeQuoteView(_CustomerView):
    def post(self, request, line_id):
        line = self.own_line(request, line_id)
        to_type = request.data.get("target_type")
        try:
            result = eligibility.assess(request.user, line, to_type=to_type)
            if to_type not in ("esim", "psim") or to_type == result.current_type:
                raise TransitionError("INVALID_TARGET", "Choose the other SIM type for this line.", 422)
            requests.raise_if_blocked(result)
            body = quote.build_quote(request.user, result, device=request.data.get("device") or {})
        except TransitionError as exc:
            return _error(exc)
        body["consent_wording_version"] = settings.SIM_CHANGE_CONSENT_VERSION
        return _no_store(Response(body))


class SimTypeChangeCreateView(_CustomerView):
    throttle_classes = [UserRateThrottle, ScopedRateThrottle]
    throttle_scope = "sim_change_create"

    def post(self, request, line_id):
        line = self.own_line(request, line_id)
        try:
            started = idempotency.begin(
                request.user, f"create:{line.uuid}", request.headers.get("Idempotency-Key", ""), request.data,
            )
        except TransitionError as exc:
            return _error(exc)
        if isinstance(started, dict):  # replay of a request that already succeeded
            return _no_store(Response(started["body"], status=started["status_code"]))

        try:
            case = requests.create_request(request.user, line, request.data, meta={
                "now": timezone.now().isoformat(), "ip": _client_ip(request),
                "user_agent": request.META.get("HTTP_USER_AGENT", ""),
            })
        except TransitionError as exc:
            idempotency.forget(started)
            return _error(exc)
        except Exception:
            idempotency.forget(started)
            raise
        body = presenter.case_view(case)
        idempotency.finish(started, status.HTTP_202_ACCEPTED, _jsonable(body))
        return _no_store(Response(body, status=status.HTTP_202_ACCEPTED))


class SimTypeChangeListView(_CustomerView):
    def get(self, request):
        cases = SimTypeChangeCase.objects.filter(owner=request.user).select_related("line", "new_sim")[:20]
        return _no_store(Response({"cases": [presenter.case_view(c) for c in cases]}))


class SimTypeChangeDetailView(_CustomerView):
    def get(self, request, case_id):
        case = self.own_case(request, case_id)
        response = _no_store(Response(presenter.case_view(case)))
        response["ETag"] = f'"{case.uuid}-v{case.version}"'
        return response


class _CaseActionView(_CustomerView):
    def act(self, request, case):  # pragma: no cover - overridden
        raise NotImplementedError

    def post(self, request, case_id):
        case = self.own_case(request, case_id)
        try:
            case = self.act(request, case)
        except TransitionError as exc:
            return _error(exc)
        return _no_store(Response(presenter.case_view(case), status=status.HTTP_202_ACCEPTED))


class SimTypeChangeConfirmView(_CaseActionView):
    def act(self, request, case):
        return requests.confirm_request(request.user, case, request.data.get("quote"))


class SimTypeChangeCancelView(_CaseActionView):
    def act(self, request, case):
        version = request.data.get("expected_version")
        return requests.cancel_request(request.user, case, int(version) if str(version or "").isdigit() else None)


class SimTypeChangeReceiptView(_CaseActionView):
    throttle_classes = [UserRateThrottle, ScopedRateThrottle]
    throttle_scope = "sim_change_receipt"

    def act(self, request, case):
        return requests.confirm_receipt(request.user, case, str(request.data.get("iccid") or ""))


class SimTypeChangeReportView(_CaseActionView):
    throttle_classes = [UserRateThrottle, ScopedRateThrottle]
    throttle_scope = "sim_change_report"

    def act(self, request, case):
        return requests.report_unauthorised(request.user, case, str(request.data.get("note") or ""))


def _jsonable(value):
    """Datetimes → ISO strings so the stored replay matches a fresh response."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
