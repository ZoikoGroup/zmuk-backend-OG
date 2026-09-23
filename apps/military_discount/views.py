from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .serializers import MilitaryDiscountEnrollmentSerializer


class MilitaryDiscountEnrollmentAPIView(APIView):
    def post(self, request):
        serializer = MilitaryDiscountEnrollmentSerializer(
            data=request.data
        )

        if serializer.is_valid():
            serializer.save()
            return Response(
                {
                    "message": "Enrollment submitted successfully"
                },
                status=status.HTTP_201_CREATED
            )

        return Response(
            {
                "message": "Validation failed",
                "errors": serializer.errors
            },
            status=status.HTTP_400_BAD_REQUEST
        )
