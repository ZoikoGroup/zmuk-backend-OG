from django.urls import path
from .views import (
    RegisterAPI,
    VerifyEmailAPI,
    LoginAPI,
    LogoutAPI,
    DashboardAPI,
    ForgotPasswordAPI,
    ResetPasswordAPI,
    UpdateUserAPI,
    SocialUserAPI,
    ChangePasswordAPI,
)

urlpatterns = [
    path("register/", RegisterAPI.as_view(), name="register"),
    path("verify/<uidb64>/<token>/", VerifyEmailAPI.as_view(), name="verify-email"),
    path("login/", LoginAPI.as_view(), name="login"),
    path("logout/", LogoutAPI.as_view(), name="logout"),
    path("dashboard/", DashboardAPI.as_view(), name="dashboard"),
    path("forgot-password/", ForgotPasswordAPI.as_view(), name="forgot-password"),
    path("reset-password/<uidb64>/<token>/", ResetPasswordAPI.as_view(), name="reset-password"),
    path("update-profile/", UpdateUserAPI.as_view(), name="update-profile"),
    path("social-user/", SocialUserAPI.as_view()),
     path("change-password/", ChangePasswordAPI.as_view(), name="change-password"),
]
