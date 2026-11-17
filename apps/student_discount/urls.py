from django.urls import path

from .views import MyStudentStatusView, StudentDiscountApplicationView, StudentPlansView

urlpatterns = [
    path(
        "",
        StudentDiscountApplicationView.as_view(),
        name="student-discount",
    ),
    path(
        "me/",
        MyStudentStatusView.as_view(),
        name="student-discount-my-status",
    ),
    path(
        "plans/",
        StudentPlansView.as_view(),
        name="student-discount-plans",
    ),
]
