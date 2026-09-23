from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .serializers import VCareOrderSerializer

class VCareOrderCreateAPIView(APIView):
    def post(self, request):
        serializer = VCareOrderSerializer(
            data={"raw_data": request.data}
        )

        if serializer.is_valid():
            serializer.save()
            return Response(
                {"message": "Order saved successfully"},
                status=status.HTTP_201_CREATED
            )

        return Response(serializer.errors, status=400)
