"""
Django settings for config project.
"""

from pathlib import Path
import os
# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-$l24e9dl==b9ak+=ql=gmh_i(@txquv-9-@p59&u-!sj$3yca1'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = ['llm-evaluation-web-apps.apps.rmhopnstkd01a.ssg.org.au']



CSRF_TRUSTED_ORIGINS = [
    "https://*.githubpreview.dev",
    "https://*.app.github.dev",
    'https://llm-evaluation-web-apps.apps.rmhopnstkd01a.ssg.org.au',
]

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_htmx',
    'encrypted_model_fields',
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django_htmx.middleware.HtmxMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'core' / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database - SQLite with data directory for persistence
DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(exist_ok=True)

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME', 'placeholder'),
        'USER': os.environ.get('DB_USER', 'placeholder'),
        'PASSWORD': os.environ.get('DB_PASSWORD', 'placeholder'),
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '5432'),
    }
}
# Celery
CELERY_BROKER_URL = 'redis://localhost:6379/0'
CELERY_RESULT_BACKEND = 'redis://localhost:6379/0'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'

# Encrypted model fields (for API keys) - Fernet key
FIELD_ENCRYPTION_KEY = 'I2ccGk4FKZLXdhkElbvMDIfHJjJoYM_FLFplLx1IiZk='

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'core' / 'static'] if (BASE_DIR / 'core' / 'static').exists() else []
STORAGES = {
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'

# File uploads - store in data directory
MEDIA_ROOT = DATA_DIR / 'uploads'
MEDIA_URL = '/media/'

# -----------------------------------------------------------------------------
# Agents service integration
# -----------------------------------------------------------------------------
# The clinical_graphs agents service is a separate deployment. Django talks to
# it over HTTP for two distinct surfaces:
#
#   - Runtime:  POST /v1/chat/completions  (configured per-ModelConfig via
#               ``api_endpoint``; used by ``core.services.llm_client``).
#   - Admin/registry:  GET /admin/registry etc. (configured here; used by
#               ``core.services.agents_client`` and the
#               ``sync_agent_registry`` management command).
#
# See ``docs/AGENTS_SERVICE_GUIDE.md`` for the full contract.
#
# Leave ``AGENTS_SERVICE_URL`` blank to disable registry sync entirely (Phase A
# deployments that only need to call /v1/chat/completions can ignore this).
AGENTS_SERVICE_URL = os.environ.get('AGENTS_SERVICE_URL', '')
AGENTS_SERVICE_ADMIN_KEY = os.environ.get('AGENTS_SERVICE_ADMIN_KEY', '')
AGENTS_SERVICE_TIMEOUT = float(os.environ.get('AGENTS_SERVICE_TIMEOUT', '30'))

# If True, the post_save signal on ModelConfig regenerates
# ``llm_providers.yaml`` (at LLM_PROVIDERS_YAML_PATH, or by default at
# ``<BASE_DIR>/dist/llm_providers.yaml``) every time a ModelConfig row is
# saved/deleted. The file is a deploy artefact consumed by the external
# agents service; Django does not read it back. Leave False in prod and
# regenerate from CI to keep the signal path lightweight.
AUTO_GENERATE_LLM_PROVIDERS_YAML = (
    os.environ.get('AUTO_GENERATE_LLM_PROVIDERS_YAML', 'false').lower() == 'true'
)
LLM_PROVIDERS_YAML_PATH = os.environ.get('LLM_PROVIDERS_YAML_PATH', '') or None
