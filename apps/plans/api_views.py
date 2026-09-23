from rest_framework.views import APIView
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

from .models import Plan, PlanCategory
from .serializers import PlanSerializer


class PlanListAPI(APIView):
    def get(self, request):
        plans = Plan.objects.filter(is_active=True)
        return Response(PlanSerializer(plans, many=True).data)


class PlanDetailAPI(APIView):
    def get(self, request, slug):
        plan = get_object_or_404(Plan, slug=slug, is_active=True)
        return Response(PlanSerializer(plan).data)


class PlanByCategoryAPI(APIView):
    def get(self, request, slug):
        category = get_object_or_404(PlanCategory, slug=slug)
        plans = Plan.objects.filter(category=category, is_active=True)
        return Response(PlanSerializer(plans, many=True).data)
