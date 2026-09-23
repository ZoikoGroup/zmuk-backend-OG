"""Fires the subscriber notification when a blog post becomes published.

Why post_save and not something simpler:

* Posts default to status='draft', so "new blog posted" is really the moment
  status flips to 'published' — which may be a later edit, not creation.
* transaction.on_commit means no email is sent if the database transaction
  rolls back. Without it, a failed save could still trigger a send.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .emails import notify_subscribers_of_post
from .models import BlogPost

logger = logging.getLogger(__name__)


@receiver(post_save, sender=BlogPost, dispatch_uid="blog_notify_subscribers_on_publish")
def notify_on_publish(sender, instance, created, raw=False, **kwargs):
    # `raw=True` during loaddata / fixture loading — never email then.
    if raw:
        return

    # Only published posts, and only once each. The notification_sent_at check
    # is repeated inside notify_subscribers_of_post as a second guard, since
    # the management command and admin action call it directly.
    if instance.status != "published":
        return
    if instance.notification_sent_at:
        return

    def _send():
        try:
            notify_subscribers_of_post(instance)
        except Exception:
            # An SMTP failure must never break saving the post.
            logger.exception(
                "Failed to send subscriber notification for blog post %s.",
                instance.pk,
            )

    transaction.on_commit(_send)
