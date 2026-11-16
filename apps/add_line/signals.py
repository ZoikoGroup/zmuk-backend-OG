"""Hooks other apps can listen to without add_line knowing about them.

Both are sent with `send_robust`, so a failing receiver is logged and never
undoes a payment or an activation.

* order_paid(sender=AddLineOrder, order): the order has just become PAID
  (sent once, after the paid state is saved, before fulfilment starts).
* line_created(sender=AddLineOrder, order, line): fulfilment has just created
  the order's line (pending activation). Sent inside the fulfilment
  transaction, so receivers may add related rows such as a PortRequest,
  which is then submitted when the line goes live.

Used by Switch & Save (apps.switch.conversion).
"""

import logging

from django.dispatch import Signal

logger = logging.getLogger("apps.add_line")

order_paid = Signal()
line_created = Signal()
# charge_event(sender=None, event): a verified Stripe charge.refunded /
# charge.dispute.* webhook event (refunds and chargebacks after payment).
charge_event = Signal()

CHARGE_EVENTS = ("charge.refunded", "charge.dispute.created", "charge.dispute.closed")


def send(signal, **kwargs):
    for receiver, result in signal.send_robust(**kwargs):
        if isinstance(result, Exception):
            logger.error("add_line signal receiver %r failed", receiver, exc_info=result)
