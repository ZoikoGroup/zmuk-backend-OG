from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.authtoken.models import Token

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.contrib.sites.shortcuts import get_current_site
from django.core.mail import send_mail
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


# ---------------- REGISTER ----------------
class RegisterAPI(APIView):
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # 🔒 User inactive until email verified
        user = serializer.save(is_active=False)

        token = default_token_generator.make_token(user)
        uid = urlsafe_base64_encode(force_bytes(user.pk))

        # ✅ Get frontend origin from React request header (dynamic)
        frontend_origin = request.headers.get(
            "X-Frontend-Origin",
            f"{request.scheme}://{request.get_host()}"  # fallback just in case
        )

        # 🔗 Verification link includes frontend_origin as query param
        verification_link = (
            f"{request.build_absolute_uri('/')[:-1]}/api/accounts/verify/{uid}/{token}/"
            f"?frontend={frontend_origin}"
        )

        send_mail(
            subject="Verify your email",
            message=f"Click the link below to verify your email:\n\n{verification_link}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )

        return Response({
            "message": "User registered successfully. Check your email for verification."
        })


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

        # ✅ Activate user
        user.is_active = True
        user.save()

        # ✅ Read frontend origin from query param passed in verification link
        frontend_origin = request.GET.get(
            "frontend",
            f"{request.scheme}://{request.get_host()}"  # fallback backend base URL
        )

        # 🔗 Redirect to frontend login with verified flag
        return redirect(f"{frontend_origin}/login?verified=1")


# ---------------- LOGIN ----------------
# class LoginAPI(APIView):
#     def post(self, request):
#         serializer = LoginSerializer(data=request.data)
#         serializer.is_valid(raise_exception=True)

#         user = serializer.validated_data
#         token, _ = Token.objects.get_or_create(user=user)

#         user_data = {
#             field.name: getattr(user, field.name)
#             for field in user._meta.fields
#         }

#         return Response({
#             "message": "Login successful",
#             "token": token.key,
#             "user": user_data
#         })

# views.py
class LoginAPI(APIView):
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data
        token, _ = Token.objects.get_or_create(user=user)

        user_data = {
            field.name: getattr(user, field.name)
            for field in user._meta.fields
        }

        # Add VC ID
        user_data['bq_enrollment_id'] = getattr(user.profile, 'bq_enrollment_id', None)

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

        email = serializer.validated_data["email"]

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({
                "message": "If the email exists, a reset link was sent."
            })

        token = default_token_generator.make_token(user)
        uid = urlsafe_base64_encode(force_bytes(user.pk))

        frontend_origin = request.headers.get(
            "X-Frontend-Origin",
            f"{request.scheme}://{request.get_host()}"
        )

        reset_link = f"{frontend_origin}/reset-password/{uid}/{token}"

        send_mail(
            subject="Reset your password",
            message=f"Click the link below to reset your password:\n\n{reset_link}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )

        return Response({
            "message": "Password reset link sent"
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

        if default_token_generator.check_token(user, token):
            user.set_password(serializer.validated_data["password"])
            user.save()
            return Response({"message": "Password reset successful"})

        return Response({"error": "Invalid or expired token"}, status=400)


# ---------------- UPDATE PROFILE ----------------
# class UpdateUserAPI(APIView):
#     permission_classes = [IsAuthenticated]

#     def put(self, request):
#         serializer = UpdateUserSerializer(
#             request.user,
#             data=request.data,
#             partial=True,
#             context={"request": request}
#         )

#         serializer.is_valid(raise_exception=True)
#         serializer.save()

#         return Response({
#             "message": "Profile updated successfully",
#             "user": serializer.data
#         })


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

        # Include bq_enrollment_id in response
        response_data = serializer.data
        response_data['bq_enrollment_id'] = getattr(request.user.profile, 'bq_enrollment_id', None)

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

        # ✅ Check if user exists
        user = User.objects.filter(email=email).first()

        if not user:
            # Create user without password
            user = User.objects.create_user(
                username=email,
                email=email,
                first_name=first_name,
                last_name=last_name,
                is_active=True  # Social users auto verified
            )
            user.set_unusable_password()
            user.save()

        # ✅ Create or get token
        token, _ = Token.objects.get_or_create(user=user)

        user_data = {
            field.name: getattr(user, field.name)
            for field in user._meta.fields
        }

        # Add VC ID (same as login)
        user_data['bq_enrollment_id'] = getattr(user.profile, 'bq_enrollment_id', None)

        return Response({
            "message": "Social login successful",
            "token": token.key,
            "user": user_data
        })


class ChangePasswordAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.set_password(serializer.validated_data["password"])
        user.save()

        # Rotate the auth token so the new session stays valid after the change.
        Token.objects.filter(user=user).delete()
        token = Token.objects.create(user=user)

        return Response({
            "message": "Password updated successfully",
            "token": token.key,
        })
