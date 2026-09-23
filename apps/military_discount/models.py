from django.db import models


class MilitaryDiscountEnrollment(models.Model):
    ID_TYPE_CHOICES = [
        ("DD Form 214", "DD Form 214"),
        ("Military Retiree ID Card", "Military Retiree ID Card"),
        ("VA Veteran ID Card", "VA Veteran ID Card"),
        ("State Issued Veteran ID", "State Issued Veteran ID"),
        ("Active Duty CAC", "Active Duty CAC"),
    ]

    full_name = models.CharField(max_length=255)
    email = models.EmailField()
    dob = models.DateField()
    id_type = models.CharField(max_length=255, choices=ID_TYPE_CHOICES)
    document_file = models.FileField(upload_to="military_documents/")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.full_name
