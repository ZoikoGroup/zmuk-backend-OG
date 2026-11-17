from rest_framework.views import APIView
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

from .models import Plan, PlanCategory
from .serializers import PlanSerializer


# Public sector and student plans are served by their own modules, only.
def public_plans():
    return Plan.objects.filter(is_active=True, audience=Plan.Audience.GENERAL)


class PlanListAPI(APIView):
    def get(self, request):
        plans = public_plans()
        return Response(PlanSerializer(plans, many=True).data)


class PlanDetailAPI(APIView):
    def get(self, request, slug):
        plan = get_object_or_404(public_plans(), slug=slug)
        return Response(PlanSerializer(plan).data)


class PlanByCategoryAPI(APIView):
    def get(self, request, slug):
        category = get_object_or_404(PlanCategory, slug=slug)
        plans = public_plans().filter(category=category)
        return Response(PlanSerializer(plans, many=True).data)
