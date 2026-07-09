from django.urls import path

from .views import StudentDiscountApplicationView

urlpatterns = [
    path(
        "",
        StudentDiscountApplicationView.as_view(),
        name="student-discount",
    ),
]