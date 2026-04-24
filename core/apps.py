from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        # Import signal handlers so they get registered with Django's dispatcher.
        # Guarded against re-import for side-effects only.
        from core import signals  # noqa: F401
