from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from .models import Job
from .serializers import JobSerializer

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def job_list_api(request):
    jobs = Job.objects.all()
    serializer = JobSerializer(jobs, many=True)
    return Response(serializer.data)
