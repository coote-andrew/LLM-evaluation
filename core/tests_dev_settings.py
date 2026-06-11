from django.test import SimpleTestCase

from config import dev_settings


class DevSettingsTests(SimpleTestCase):
    def test_dev_settings_use_local_sqlite(self):
        default_db = dev_settings.DATABASES["default"]

        self.assertEqual(default_db["ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(default_db["NAME"], dev_settings.BASE_DIR / "db.sqlite3")

    def test_dev_settings_do_not_require_celery_infrastructure(self):
        self.assertEqual(dev_settings.CELERY_BROKER_URL, "memory://")
        self.assertEqual(dev_settings.CELERY_RESULT_BACKEND, "cache+memory://")
        self.assertTrue(dev_settings.CELERY_TASK_ALWAYS_EAGER)
        self.assertTrue(dev_settings.CELERY_TASK_EAGER_PROPAGATES)
