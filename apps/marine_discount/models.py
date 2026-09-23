from django.db import models


class MarineDiscountEnrollment(models.Model):

    DOCUMENT_CHOICES = [
        ("Membership Certificate / ID Card", "Membership Certificate / ID Card"),
        ("Recent Activity Proof", "Recent Activity Proof"),
    ]

    full_name = models.CharField(max_length=255)
    ngo_name = models.CharField(max_length=255)
    email = models.EmailField()
    dob = models.DateField()
    id_type = models.CharField(max_length=255)
    document_file = models.FileField(upload_to="marine_documents/")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.full_name
