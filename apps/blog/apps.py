from django.apps import AppConfig


class BlogConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.blog'

    def ready(self):
        # Importing here (not at module top) is required — models are not
        # loaded yet when this module is first imported. Without this method
        # the post_save receiver is never connected and no emails are sent.
        from . import signals  # noqa: F401
