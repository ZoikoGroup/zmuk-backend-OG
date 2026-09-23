from django.db import models


class JobApplication(models.Model):

    POSITION_CHOICES = [
        ("Software Development", "Software Development"),
        ("Customer Operations & Loyalty Lifecycle Management", "Customer Operations & Loyalty Lifecycle Management"),
        ("IoT, SIM, & Telematics Engineering", "IoT, SIM, & Telematics Engineering"),
        ("Strategic Partnerships & B2B Sales", "Strategic Partnerships & B2B Sales"),
        ("Business Development Executives", "Business Development Executives"),
        ("Analytics, Insights & Performance Strategy", "Analytics, Insights & Performance Strategy"),
    ]

    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=20)

    position_applied = models.CharField(
        max_length=200,
        choices=POSITION_CHOICES
    )

    resume = models.FileField(upload_to="resumes/")
    applied_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.full_name} - {self.position_applied}"