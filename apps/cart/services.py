"""
Guest-cart bridge for checkout.

Flow:
  1. Anonymous visitor adds items → cart tied to request.session.session_key.
  2. They log in / register at checkout.
  3. merge_guest_cart() folds the session cart into their user cart.
Best-effort: never raise, never block authentication.
"""
from django.db import transaction

from .models import Cart, CartLine


def get_or_create_cart(request):
    """Return the right cart for this request (user cart if logged in, else session cart)."""
    if request.user.is_authenticated:
        cart, _ = Cart.objects.get_or_create(user=request.user)
        return cart
    if not request.session.session_key:
        request.session.create()
    cart, _ = Cart.objects.get_or_create(session_key=request.session.session_key, user__isnull=True)
    return cart


@transaction.atomic
def merge_guest_cart(request, user):
    session_key = request.session.session_key
    if not session_key:
        return
    guest = Cart.objects.filter(session_key=session_key, user__isnull=True).select_related().first()
    if not guest:
        return

    user_cart, _ = Cart.objects.get_or_create(user=user)
    for line in guest.lines.all():
        existing = user_cart.lines.filter(product_id=line.product_id).first()
        if existing:
            existing.quantity += line.quantity   # combine quantities
            existing.save(update_fields=["quantity"])
        else:
            line.cart = user_cart                # move the line over
            line.save(update_fields=["cart"])
    guest.delete()
