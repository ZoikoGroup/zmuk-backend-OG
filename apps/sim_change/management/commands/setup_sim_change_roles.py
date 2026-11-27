"""Create / refresh the Change SIM type staff groups (also runs after every migrate).

    python manage.py setup_sim_change_roles
"""

from django.core.management.base import BaseCommand

from apps.sim_change.services.roles import ensure_roles


class Command(BaseCommand):
    help = "Create or update the SIM change staff role groups."

    def handle(self, *args, **options):
        for name, created in ensure_roles().items():
            self.stdout.write(f"{'Created' if created else 'Updated'}: {name}")
