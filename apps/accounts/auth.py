"""
JWT delivered via httpOnly cookies. httponly = invisible to JS = XSS can't steal it.
SameSite=Lax + CSRF_TRUSTED_ORIGINS defend against CSRF.
"""
from django.conf import settings
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import RefreshToken

ACCESS_COOKIE = "zoiko_access"
REFRESH_COOKIE = "zoiko_refresh"
ACCESS_MAX_AGE = 15 * 60
REFRESH_MAX_AGE = 30 * 24 * 60 * 60


class CookieJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        if self.get_header(request) is not None:
            return super().authenticate(request)
        raw = request.COOKIES.get(ACCESS_COOKIE)
        if not raw:
            return None
        try:
            validated = self.get_validated_token(raw)
        except (InvalidToken, TokenError):
            return None
        return self.get_user(validated), validated


def cookie_kwargs():
    return {
        "httponly": True,
        "secure": not settings.DEBUG,
        "samesite": "Lax",
        "path": "/",
        "domain": getattr(settings, "AUTH_COOKIE_DOMAIN", None),
    }


def set_auth_cookies(response, user, remember=False):
    refresh = RefreshToken.for_user(user)
    response.set_cookie(ACCESS_COOKIE, str(refresh.access_token), max_age=ACCESS_MAX_AGE, **cookie_kwargs())
    response.set_cookie(
        REFRESH_COOKIE, str(refresh),
        max_age=REFRESH_MAX_AGE if remember else None,
        **cookie_kwargs(),
    )
    return response


def clear_auth_cookies(response):
    kw = cookie_kwargs()
    response.delete_cookie(ACCESS_COOKIE, path=kw["path"], domain=kw["domain"], samesite=kw["samesite"])
    response.delete_cookie(REFRESH_COOKIE, path=kw["path"], domain=kw["domain"], samesite=kw["samesite"])
    return response
