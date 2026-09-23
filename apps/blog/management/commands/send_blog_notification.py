"""Send the new-post notification for a blog post manually.

Usage:

    # Normal send (skips if already notified)
    python manage.py send_blog_notification my-post-slug

    # Count recipients without sending anything
    python manage.py send_blog_notification my-post-slug --dry-run

    # Re-send even though it was already notified
    python manage.py send_blog_notification my-post-slug --force

    # Send to one address only, to check formatting before the real send
    python manage.py send_blog_notification my-post-slug --test-email you@example.com

Use this instead of the automatic signal once you have more than about 100
subscribers — see the scaling note in apps/blog/emails.py.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.blog.emails import notify_subscribers_of_post, build_message
from apps.blog.models import BlogPost


class Command(BaseCommand):
    help = "Email subscribers about a published blog post."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Slug of the blog post.")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Send even if this post was already notified.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the recipient count without sending.",
        )
        parser.add_argument(
            "--test-email",
            help="Send a single copy to this address only. Does not mark the "
                 "post as notified and does not touch your subscriber list.",
        )

    def handle(self, *args, **options):
        slug = options["slug"]

        try:
            post = BlogPost.objects.get(slug=slug)
        except BlogPost.DoesNotExist:
            raise CommandError(f"No blog post with slug '{slug}'.")

        if post.status != "published":
            raise CommandError(
                f"Post '{slug}' has status '{post.status}'. "
                "Only published posts can be sent."
            )

        # ── Single test copy ────────────────────────────────────────────────
        test_email = options.get("test_email")
        if test_email:
            message = build_message(post, test_email)
            sent = message.send(fail_silently=False)
            if sent:
                self.stdout.write(self.style.SUCCESS(f"Test email sent to {test_email}."))
            else:
                self.stdout.write(self.style.ERROR("Test email was not sent."))
            return

        # ── Real send ──────────────────────────────────────────────────────
        if post.notification_sent_at and not options["force"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Post '{slug}' was already notified at "
                    f"{post.notification_sent_at}. Use --force to re-send."
                )
            )
            return

        count = notify_subscribers_of_post(
            post,
            force=options["force"],
            dry_run=options["dry_run"],
        )

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(f"DRY RUN: would email {count} recipients.")
            )
        elif count:
            self.stdout.write(
                self.style.SUCCESS(f"Sent notification to {count} recipients.")
            )
        else:
            self.stdout.write(
                self.style.ERROR(
                    "Nothing was sent. Check the log for the reason "
                    "(no active subscribers, DEFAULT_FROM_EMAIL unset, or SMTP failure)."
                )
            )
