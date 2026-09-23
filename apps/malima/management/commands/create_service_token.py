"""
Create the service user + token used by the Next.js proxy.

Usage:
    python manage.py create_service_token
    python manage.py create_service_token --username nextjs-checkout
    python manage.py create_service_token --rotate     # generate a fresh token

Prints the token value to stdout. Copy it into DJANGO_API_TOKEN in your
Next.js .env.local (server-only). The token never appears anywhere else
unless you run this command again with --rotate.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Create or rotate the API token used by the Next.js checkout proxy."

    def add_arguments(self, parser):
        parser.add_argument(
            "--username", default="nextjs-checkout",
            help="Service user to attach the token to (created if missing).",
        )
        parser.add_argument(
            "--rotate", action="store_true",
            help="Delete any existing token and mint a fresh one.",
        )

    def handle(self, *args, **opts):
        try:
            from rest_framework.authtoken.models import Token
        except ImportError as e:
            raise CommandError(
                "rest_framework.authtoken is not installed. Add it to "
                "INSTALLED_APPS and run `migrate authtoken` first."
            ) from e

        User = get_user_model()
        # Some custom user models (incl. AbstractBaseUser subclasses with
        # tightened constraints) require last_login and date_joined to be
        # non-null at insert time, so we set them explicitly. Extra defaults
        # are harmless for the standard auth.User.
        defaults = {
            "is_active": True,
            "last_login": timezone.now(),
        }
        # Set date_joined if the field exists on this user model.
        field_names = {f.name for f in User._meta.get_fields()}
        if "date_joined" in field_names:
            defaults["date_joined"] = timezone.now()
        # Some custom models also have an `email` field that's required.
        # Populate with a placeholder if so — the address never sends mail.
        if "email" in field_names:
            defaults["email"] = f"{opts['username']}@service.local"

        user, created_user = User.objects.get_or_create(
            username=opts["username"],
            defaults=defaults,
        )
        if created_user:
            # Service account — no password login.
            user.set_unusable_password()
            user.save()

        if opts["rotate"]:
            Token.objects.filter(user=user).delete()

        token, created = Token.objects.get_or_create(user=user)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"{'Created' if created else 'Existing'} token for user "
            f"'{user.username}':"
        ))
        self.stdout.write("")
        self.stdout.write(f"  {token.key}")
        self.stdout.write("")
        self.stdout.write(
            "Add this to your Next.js .env.local (server-only — never commit):"
        )
        self.stdout.write("")
        self.stdout.write(f"  DJANGO_API_TOKEN={token.key}")
        self.stdout.write("")
        if not created and not opts["rotate"]:
            self.stdout.write(self.style.WARNING(
                "(This is the same value as last time. Use --rotate to mint a new one.)"
            ))