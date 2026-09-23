from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .serializers import RequestDemoSerializer

class RequestDemoView(APIView):
    def post(self, request):
        data = request.data

        serializer = RequestDemoSerializer(data={
            "first_name": data.get("firstName"),
            "last_name": data.get("lastName"),
            "email": data.get("email"),
            "company": data.get("company"),
            "org_type": data.get("orgType"),
            "deployment_size": data.get("deploymentSize"),
            "updates": data.get("updates"),
        })

        if serializer.is_valid():
            serializer.save()
            return Response({"message": "Demo request submitted"}, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)