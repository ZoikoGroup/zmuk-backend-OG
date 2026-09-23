from django.conf import settings
from django.db import models


class Cart(models.Model):
    """
    A cart is owned EITHER by a logged-in user OR an anonymous session key.
    On login/register we merge the session cart into the user cart.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name="cart",
    )
    session_key = models.CharField(max_length=40, null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Cart(user={self.user_id}, session={self.session_key})"

    @property
    def total(self):
        return sum(line.subtotal for line in self.lines.all())


class CartLine(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="lines")
    product_id = models.CharField(max_length=64)
    name = models.CharField(max_length=255)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)

    class Meta:
        unique_together = ("cart", "product_id")

    @property
    def subtotal(self):
        return self.unit_price * self.quantity
