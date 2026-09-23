from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser

from .models import JobApplication
from .serializers import JobApplicationSerializer


class JobApplicationCreateView(APIView):

    parser_classes = (MultiPartParser, FormParser)

    def post(self, request):

        serializer = JobApplicationSerializer(data=request.data)

        if serializer.is_valid():
            serializer.save()
            return Response(
                {"message": "Application submitted successfully"},
                status=status.HTTP_201_CREATED
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)