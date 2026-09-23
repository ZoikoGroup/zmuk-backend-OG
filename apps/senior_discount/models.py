from django.db import models

class SeniorCitizenDiscount(models.Model):
    full_name = models.CharField(max_length=255)
    email = models.EmailField()
    dob = models.DateField()
    id_type = models.CharField(max_length=100)
    document_file = models.FileField(upload_to="senior_documents/")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.full_name
