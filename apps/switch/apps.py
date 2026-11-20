from django.apps import AppConfig


class SwitchConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.switch"
    label = "switch"
    verbose_name = "Number Switch"

    def ready(self):
        from . import wording  # noqa: F401  (registers the consent wording check)
        from .conversion import connect

        connect()
