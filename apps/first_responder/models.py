from django.db import models

class FirstResponderApplication(models.Model):

    EMPLOYMENT_STATUS = (
        ("Active", "Active"),
        ("Retired", "Retired"),
    )

    full_name = models.CharField(max_length=255)
    dob = models.DateField()
    employment_status = models.CharField(max_length=20, choices=EMPLOYMENT_STATUS)
    agency_name = models.CharField(max_length=255)
    email = models.EmailField()
    position = models.CharField(max_length=100)
    proof_type = models.CharField(max_length=255)
    document_file = models.FileField(upload_to="first_responder_docs/")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.full_name
