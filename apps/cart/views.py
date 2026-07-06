from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CartLine
from .services import get_or_create_cart


def _serialize(cart):
    return {
        "id": cart.id,
        "total": str(cart.total),
        "lines": [
            {"product_id": l.product_id, "name": l.name,
             "unit_price": str(l.unit_price), "quantity": l.quantity,
             "subtotal": str(l.subtotal)}
            for l in cart.lines.all()
        ],
    }


class CartView(APIView):
    """GET current cart, POST to add/update a line. Works for guests and users."""
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(_serialize(get_or_create_cart(request)))

    def post(self, request):
        cart = get_or_create_cart(request)
        d = request.data
        line, created = CartLine.objects.get_or_create(
            cart=cart, product_id=d["product_id"],
            defaults={"name": d.get("name", ""), "unit_price": d.get("unit_price", 0),
                      "quantity": d.get("quantity", 1)},
        )
        if not created:
            line.quantity = d.get("quantity", line.quantity)
            line.save(update_fields=["quantity"])
        return Response(_serialize(cart), status=status.HTTP_200_OK)


class CheckoutView(APIView):
    """Checkout REQUIRES login — this is where guest carts become user carts."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        cart = get_or_create_cart(request)
        if not cart.lines.exists():
            return Response({"detail": "Cart is empty."}, status=400)
        # TODO: create order, take payment, etc.
        return Response({"detail": "Order placed.", "total": str(cart.total)})

class ChangePasswordAPI(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.set_password(serializer.validated_data["password"])
        user.save()

        # Rotate the auth token so the new session stays valid after the change.
        Token.objects.filter(user=user).delete()
        token = Token.objects.create(user=user)

        return Response({
            "message": "Password updated successfully",
            "token": token.key,
        })
