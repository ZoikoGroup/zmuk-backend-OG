from rest_framework import serializers
from .models import Job


class JobSerializer(serializers.ModelSerializer):
    technologies = serializers.SerializerMethodField()
    shortDescription = serializers.CharField(
        source='short_description',
        read_only=True
    )
    department_display = serializers.CharField(
        source='get_department_display',
        read_only=True
    )

    class Meta:
        model = Job
        fields = [
            'id',
            'title',
            'location',
            'technologies',
            'shortDescription',
            'description',
            'department',
            'department_display',
        ]

    def get_technologies(self, obj):
        if not obj.technologies:
            return []
        return [tech.strip() for tech in obj.technologies.split(',')]