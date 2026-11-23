from django.utils import timezone

from .models import Offer


def is_live(offer, now=None):
    """Is this offer shown to customers right now?

    Published and inside its start/end times. A coupon offer is shown only
    while its coupon still works (Coupon.is_valid(): active, inside its own
    dates, under its usage limit), so the card disappears when the code would
    stop working at checkout. A coupon assigned to users since the offer was
    saved is never shown, so a personal code can't leak.
    """
    now = now or timezone.now()
    if not (offer.is_published and offer.starts_at <= now < offer.ends_at):
        return False
    if offer.mechanism == Offer.Mechanism.COUPON:
        coupon = offer.coupon
        if coupon is None or coupon.users.all():  # users are prefetched by live_offers()
            return False
        return coupon.is_valid()[0]
    return True


def live_offers(now=None):
    """Offers to show right now, most important first."""
    now = now or timezone.now()
    candidates = (
        Offer.objects.filter(is_published=True, starts_at__lte=now, ends_at__gt=now)
        .select_related("coupon")
        .prefetch_related("coupon__users")
        .order_by("priority", "ends_at", "pk")
    )
    return [offer for offer in candidates if is_live(offer, now)]
