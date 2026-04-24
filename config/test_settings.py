"""
Django settings for running the test suite.

Inherits everything from ``config.settings`` but swaps the database to an
in-memory SQLite so tests can run without Postgres (and without a ``.env``).

Usage:
    python manage.py test --settings=config.test_settings

CI and local developers should use this module; production deployment continues
to use ``config.settings`` with Postgres.
"""

from config.settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Speeds up tests that don't need persistent broker behaviour.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Keep YAML auto-regeneration OFF during tests — individual signal tests
# flip it on with override_settings.
AUTO_GENERATE_LLM_PROVIDERS_YAML = False

# Never hit a real agents service during tests — individual tests mock the
# httpx transport. A blank URL causes the client to raise a clear error if
# someone forgets to patch it.
AGENTS_SERVICE_URL = ""
AGENTS_SERVICE_ADMIN_KEY = "test-admin-key"
AGENTS_SERVICE_TIMEOUT = 5.0
