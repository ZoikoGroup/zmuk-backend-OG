from django.db import models

# Create your models here.
from django.db import models


class ContactMessage(models.Model):

    SUBJECT_CHOICES = [
        ("General Enquiry", "General Enquiry"),
        ("Billing", "Billing"),
        ("Technical Support", "Technical Support"),
        ("Roaming & International", "Roaming & International"),
        ("Complaints", "Complaints"),
        ("Other", "Other"),
    ]

    name = models.CharField(max_length=150)

    email = models.EmailField()

    phone = models.CharField(max_length=20)

    subject = models.CharField(
        max_length=100,
        choices=SUBJECT_CHOICES
    )

    message = models.TextField()

    savePref = models.BooleanField(default=False)

    newsletter = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name