from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _ensure_roles(sender, **kwargs):
    from .services.roles import ensure_roles

    ensure_roles()


class SimChangeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sim_change"
    label = "sim_change"
    verbose_name = "Change SIM type"

    def ready(self):
        # Runs after auth has created this app's permissions for the same migrate.
        post_migrate.connect(_ensure_roles, sender=self, dispatch_uid="sim_change_ensure_roles")
