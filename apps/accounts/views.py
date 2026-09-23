import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.authtoken.models import Token

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.conf import settings
from django.shortcuts import redirect

from .serializers import (
    RegisterSerializer,
    LoginSerializer,
    ForgotPasswordSerializer,
    ResetPasswordSerializer,
    UpdateUserSerializer,
    ChangePasswordSerializer,
)
from .utils import get_safe_frontend_origin

logger = logging.getLogger("apps.accounts")


# ── Email helper ──────────────────────────────────────────────────────────────

def _send_html_email(subject, template_name, context, recipient):
    """Send a branded HTML email. Falls back to plain text automatically.
    Returns True on success, False on failure (logged — never raises)."""
    try:
        html_body = render_to_string(template_name, context)
        text_body = strip_tags(html_body)
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@zoikomobile.co.uk"

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=from_email,
            to=[recipient],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send()
        return True
    except Exception:
        logger.exception("Failed to send email '%s' to %s", subject, recipient)
        return False


# ---------------- REGISTER ----------------

class RegisterAPI(APIView):
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Save user as inactive until email is verified.
        # serializer.save() handles both new users and re-registration of
        # inactive (unverified) accounts — in both cases it returns the user.
        user = serializer.save(is_active=False)

        token = default_token_generator.make_token(user)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        frontend_origin = get_safe_frontend_origin(request)

        verification_link = (
            f"{settings.BACKEND_URL}/api/accounts/verify/{uid}/{token}/"
            f"?frontend={frontend_origin}"
        )

        email_sent = _send_html_email(
            subject="Verify your email – Zoiko Mobile",
            template_name="emails/verify_email.html",
            context={
                "first_name": user.first_name or user.username,
                "verification_link": verification_link,
            },
            recipient=user.email,
        )

        if email_sent:
            return Response({
                "message": (
                    "Account created! Check your email for a verification link. "
                    "If you registered before and missed it, we've resent it."
                )
            })
        else:
            # User row is saved — don't block them, just warn about email failure.
            # Admins can manually activate or resend from Django admin.
            return Response({
                "message": (
                    "Account created but we couldn't send the verification email right now. "
                    "Please contact support or try registering again in a few minutes."
                ),
                "email_failed": True,
            }, status=207)  # 207 Multi-Status: partial success


# ---------------- VERIFY EMAIL ----------------

class VerifyEmailAPI(APIView):
    def get(self, request, uidb64, token):
        try:
            uid = force_str(urlsafe_base64_decode(uidb64))
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response({"error": "Invalid link"}, status=400)

        if not default_token_generator.check_token(user, token):
            return Response({"error": "Invalid or expired token"}, status=400)

        user.is_active = True
        user.save()

        requested = request.GET.get("frontend", "").rstrip("/")
        allowed = getattr(settings, "FRONTEND_ALLOWED_ORIGINS", [])
        if requested in allowed:
            frontend_origin = requested
        else:
            frontend_origin = getattr(settings, "FRONTEND_URL", "").rstrip("/")

        return redirect(f"{frontend_origin}/login?verified=1")


# ---------------- LOGIN ----------------

class LoginAPI(APIView):
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data
        token, _ = Token.objects.get_or_create(user=user)

        return Response({
            "message": "Login successful",
            "token": token.key,
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "bq_enrollment_id": getattr(user.profile, "bq_enrollment_id", None),
            }
        })


# ---------------- LOGOUT ----------------

class LogoutAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        request.user.auth_token.delete()
        return Response({"message": "Logged out successfully"})


# ---------------- DASHBOARD ----------------

class DashboardAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        return Response({
            field.name: getattr(user, field.name)
            for field in user._meta.fields
        })


# ---------------- FORGOT PASSWORD ----------------

class ForgotPasswordAPI(APIView):
    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].strip().lower()

        # Always return the same response regardless of whether the email exists
        # — prevents user enumeration (someone probing which emails are registered).
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return Response({
                "message": "If that email is registered, a reset link has been sent."
            })

        token = default_token_generator.make_token(user)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        frontend_origin = get_safe_frontend_origin(request)
        reset_link = f"{frontend_origin}/reset-password/{uid}/{token}"

        _send_html_email(
            subject="Reset your Zoiko Mobile password",
            template_name="emails/reset_password.html",
            context={
                "first_name": user.first_name or user.username,
                "reset_link": reset_link,
            },
            recipient=user.email,
        )

        # Don't tell the user whether email sending succeeded — prevents
        # confirming that the email is registered.
        return Response({
            "message": "If that email is registered, a reset link has been sent."
        })


# ---------------- RESET PASSWORD ----------------

class ResetPasswordAPI(APIView):
    def post(self, request, uidb64, token):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            uid = force_str(urlsafe_base64_decode(uidb64))
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response({"error": "Invalid link"}, status=400)

        if not default_token_generator.check_token(user, token):
            return Response({"error": "Invalid or expired token"}, status=400)

        try:
            validate_password(serializer.validated_data["password"], user=user)
        except ValidationError as e:
            return Response({"error": list(e.messages)}, status=400)

        user.set_password(serializer.validated_data["password"])
        user.save()
        return Response({"message": "Password reset successful"})


# ---------------- UPDATE PROFILE ----------------

class UpdateUserAPI(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request):
        serializer = UpdateUserSerializer(
            request.user,
            data=request.data,
            partial=True,
            context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        response_data = serializer.data
        response_data["bq_enrollment_id"] = getattr(request.user.profile, "bq_enrollment_id", None)

        return Response({
            "message": "Profile updated successfully",
            "user": response_data
        })


# ---------------- SOCIAL LOGIN / REGISTER ----------------

class SocialUserAPI(APIView):
    def post(self, request):
        email = request.data.get("email")
        first_name = request.data.get("first_name", "")
        last_name = request.data.get("last_name", "")

        if not email:
            return Response({"error": "Email is required"}, status=400)

        user = User.objects.filter(email=email).first()

        if not user:
            user = User.objects.create_user(
                username=email,
                email=email,
                first_name=first_name,
                last_name=last_name,
                is_active=True,
            )
            user.set_unusable_password()
            user.save()

        token, _ = Token.objects.get_or_create(user=user)

        return Response({
            "message": "Social login successful",
            "token": token.key,
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "bq_enrollment_id": getattr(user.profile, "bq_enrollment_id", None),
            }
        })


# ---------------- CHANGE PASSWORD ----------------

class ChangePasswordAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user

        try:
            validate_password(serializer.validated_data["password"], user=user)
        except ValidationError as e:
            return Response({"error": list(e.messages)}, status=400)

        user.set_password(serializer.validated_data["password"])
        user.save()

        # Rotate auth token so old sessions are invalidated.
        Token.objects.filter(user=user).delete()
        token = Token.objects.create(user=user)

        return Response({
            "message": "Password updated successfully",
            "token": token.key,
        })
