from django.db import models
from django.contrib.auth.models import User

class Job(models.Model):

    DEPARTMENT_CHOICES = [
           ('software_development', 'Software Development'),
    ('customer_operations', 'Customer Operations & Loyalty Lifecycle Management'),
    ('iot_engineering', 'IoT, SIM, & Telematics Engineering'),
    ('strategic_partnerships', 'Strategic Partnerships & B2B Sales'),
    ('business_development', 'Business Development Executives'),
    ('analytics_strategy', 'Analytics, Insights & Performance Strategy'),
    ]

    title = models.CharField(max_length=200)
    short_description = models.CharField( max_length=300,default="Short description will be updated")
    description = models.TextField()
    location = models.CharField(max_length=100)

    department = models.CharField(   
         max_length=50,
    choices=DEPARTMENT_CHOICES,
    default='software_development'
    )

    technologies = models.CharField(
    max_length=300,
    null=True,
    blank=True
)
    experience = models.CharField(max_length=100)
    positions = models.PositiveIntegerField(default=1)
    salary = models.CharField(max_length=100, blank=True)
    status = models.BooleanField(default=True)
    posted_by = models.ForeignKey(User, on_delete=models.CASCADE)
    posted_at = models.DateTimeField(auto_now_add=True)


    def __str__(self):
        return self.title
