"""Subscriber notification emails for newly published blog posts.

Design notes:

* One message per subscriber. Putting every address in a single `To:` or `Cc:`
  would expose your whole subscriber list to every recipient — a GDPR breach.
* All messages go over a single SMTP connection, opened once and reused, rather
  than reconnecting per subscriber.
* `notification_sent_at` on the post guards against duplicates, so re-saving a
  published post in the admin does not re-email everyone.
* Sending is synchronous. See the warning at the bottom of this file about
  subscriber counts.
"""

import logging

from django.conf import settings
from django.core.mail import get_connection, EmailMultiAlternatives
from django.core.signing import dumps as sign_dumps
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)

# How many messages to hand to the SMTP connection at once. Most providers
# throttle or drop connections that fire hundreds of messages in one go.
BATCH_SIZE = getattr(settings, "BLOG_NOTIFICATION_BATCH_SIZE", 40)


def _post_url(post) -> str:
    """Public URL of the post on the Next.js frontend.

    Built from FRONTEND_URL rather than request.build_absolute_uri() so the
    link is correct no matter which host the admin request arrived on.
    """
    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}/blogs/{post.slug}"


def _image_url(post) -> str | None:
    """Absolute URL for the featured image, or None."""
    if not post.featured_image:
        return None
    url = post.featured_image.url
    if url.startswith("http"):
        return url
    base = (getattr(settings, "BACKEND_URL", "") or "").rstrip("/")
    return f"{base}{url}"


def _unsubscribe_url(email: str) -> str:
    """Signed, tamper-proof unsubscribe link.

    Uses Django's signing rather than a stored token, so no extra model field
    is needed. The signature is tied to SECRET_KEY — rotating SECRET_KEY
    invalidates existing links, which is acceptable.
    """
    token = sign_dumps({"email": email}, salt="newsletter.unsubscribe")
    base = (getattr(settings, "BACKEND_URL", "") or "").rstrip("/")
    return f"{base}/api/newsletter/unsubscribe/{token}/"


def build_message(post, subscriber_email: str, connection=None) -> EmailMultiAlternatives:
    """Render one personalised email for one subscriber."""
    context = {
        "post": post,
        "post_url": _post_url(post),
        "image_url": _image_url(post),
        "unsubscribe_url": _unsubscribe_url(subscriber_email),
        "site_name": getattr(settings, "SITE_NAME", "Zoiko Mobile"),
        "frontend_url": (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/"),
    }

    subject = render_to_string("emails/new_blog_post_subject.txt", context).strip()
    # A newline in a Subject header breaks the email (header injection).
    subject = " ".join(subject.split())

    html_body = render_to_string("emails/new_blog_post.html", context)

    try:
        text_body = render_to_string("emails/new_blog_post.txt", context)
    except Exception:
        # Fall back to a stripped version of the HTML rather than sending
        # HTML-only, which trips spam filters.
        text_body = strip_tags(html_body)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[subscriber_email],
        connection=connection,
    )
    message.attach_alternative(html_body, "text/html")

    # Lets Gmail/Outlook show a native unsubscribe button, which materially
    # improves deliverability.
    message.extra_headers = {
        "List-Unsubscribe": f"<{_unsubscribe_url(subscriber_email)}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    return message


def notify_subscribers_of_post(post, *, force: bool = False, dry_run: bool = False) -> int:
    """Email all active subscribers about a published post.

    Returns the number of messages sent (or that would be sent, for dry_run).

    Skips silently and returns 0 when:
      * the post is not published
      * the post has already been notified and force is False
      * BLOG_NOTIFY_ON_PUBLISH is disabled
      * there are no active subscribers
    """
    # Imported here rather than at module level to avoid an app-loading
    # circular import between blog and newsletter.
    from apps.newsletter.models import Subscriber

    if post.status != "published":
        logger.info("Blog notification skipped: post %s is not published.", post.pk)
        return 0

    if post.notification_sent_at and not force:
        logger.info(
            "Blog notification skipped: post %s already notified at %s.",
            post.pk,
            post.notification_sent_at,
        )
        return 0

    if not getattr(settings, "BLOG_NOTIFY_ON_PUBLISH", True) and not force:
        logger.info("Blog notification skipped: BLOG_NOTIFY_ON_PUBLISH is disabled.")
        return 0

    if not settings.DEFAULT_FROM_EMAIL:
        logger.error("Blog notification aborted: DEFAULT_FROM_EMAIL is not set.")
        return 0

    emails = list(
        Subscriber.objects.filter(is_active=True)
        .values_list("email", flat=True)
        .distinct()
    )

    # Optional internal copy, e.g. BLOG_NOTIFICATION_ADMIN_EMAIL in .env
    admin_copy = getattr(settings, "BLOG_NOTIFICATION_ADMIN_EMAIL", None)
    if admin_copy and admin_copy not in emails:
        emails.append(admin_copy)

    if not emails:
        logger.info("Blog notification skipped: no active subscribers.")
        return 0

    if dry_run:
        logger.info("Blog notification DRY RUN: would email %s recipients.", len(emails))
        return len(emails)

    sent = 0
    connection = get_connection(fail_silently=False)

    try:
        connection.open()
        for start in range(0, len(emails), BATCH_SIZE):
            batch = emails[start:start + BATCH_SIZE]
            messages = [build_message(post, addr, connection) for addr in batch]
            try:
                sent += connection.send_messages(messages) or 0
            except Exception:
                # One bad batch must not abort the rest of the send.
                logger.exception(
                    "Blog notification batch failed for post %s (recipients %s-%s).",
                    post.pk, start, start + len(batch),
                )
    finally:
        try:
            connection.close()
        except Exception:
            pass

    if sent:
        # queryset.update() rather than post.save() — save() would fire
        # post_save again and re-enter the signal.
        type(post).objects.filter(pk=post.pk).update(
            notification_sent_at=timezone.now()
        )
        logger.info("Blog notification sent for post %s to %s recipients.", post.pk, sent)
    else:
        logger.error("Blog notification sent 0 messages for post %s.", post.pk)

    return sent


# ─────────────────────────────────────────────────────────────────────────────
# SCALING WARNING
# ─────────────────────────────────────────────────────────────────────────────
# This runs inside the request that saved the post, so the admin page waits for
# every email to be sent. Rough guide at ~0.3s per SMTP message:
#
#     10 subscribers    ~3s      fine
#     100 subscribers   ~30s     slow, but survivable
#     500 subscribers   ~2.5min  gunicorn will time out and the admin 502s
#
# Above roughly 100 subscribers, stop sending from the signal:
#   1. Set BLOG_NOTIFY_ON_PUBLISH=False in .env
#   2. Publish the post normally
#   3. Run: python manage.py send_blog_notification <slug>
#
# The long-term fix is Celery + Redis, or an email provider with a bulk API
# (SendGrid, Mailgun, Resend) that accepts all recipients in one HTTP call.
