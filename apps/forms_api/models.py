from django.db import models

class RequestForm(models.Model):
    # REQUEST_TYPE_CHOICES = [
    #     ('general', 'General'),
    #     ('support', 'Support'),
    #     ('sales', 'Sales'),
    # ]

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField()
    message = models.TextField()
    request_type = models.CharField(max_length=50)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name}"