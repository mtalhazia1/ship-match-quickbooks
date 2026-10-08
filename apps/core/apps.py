from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"

    def ready(self):
        # django.contrib.admin is listed first, so every app's admin.py is registered by now
        from .admin import lock_client_data

        lock_client_data()
