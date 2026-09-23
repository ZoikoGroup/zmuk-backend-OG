from django.core.signing import TimestampSigner
from django.conf import settings

signer = TimestampSigner()

def generate_token(email):
    return signer.sign(email)

def verify_token(token, max_age=86400):
    return signer.unsign(token, max_age=max_age)


def get_safe_frontend_origin(request):
    """
    Resolve which frontend origin to build register/reset links against.

    The frontend sends its own origin via the X-Frontend-Origin header so the
    same backend works for local dev, staging, and production. But a raw
    client-supplied header can't be trusted for a link that goes out in an
    email (someone could send X-Frontend-Origin: https://evil.example and
    get us to email a phishing link). So we only accept it if it's in
    settings.FRONTEND_ALLOWED_ORIGINS; otherwise we fall back to
    settings.FRONTEND_URL (the canonical production frontend).
    """
    origin = request.headers.get("X-Frontend-Origin", "").rstrip("/")
    allowed = getattr(settings, "FRONTEND_ALLOWED_ORIGINS", [])
    if origin in allowed:
        return origin
    return getattr(settings, "FRONTEND_URL", "").rstrip("/")