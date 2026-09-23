from rest_framework.views import APIView

from rest_framework.response import Response

from rest_framework import status

from .serializers import (
    TravelEcosystemPartnerInquirySerializer
)


class TravelEcosystemPartnerInquiryCreateAPIView(
    APIView
):

    def post(self, request):

        serializer = (
            TravelEcosystemPartnerInquirySerializer(
                data=request.data
            )
        )

        if serializer.is_valid():

            serializer.save()

            return Response(
                {
                    "success": True,
                    "message": "Inquiry submitted successfully",
                    "data": serializer.data,
                },
                status=status.HTTP_201_CREATED
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )