import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('SECRET_KEY', 'unsafe-default-key')
JWT_PUBLIC_KEY = os.environ.get('JWT_PUBLIC_KEY', 'unsafe-jwt-public-key')
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
DEBUG = os.environ.get('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = ['*']

# Allow plain HTTP webhook dispatch in test/dev environments.
# When False (production), send_webhook uses the PinnedIPAdapter with TLS SNI pinning.
WEBHOOK_ALLOW_HTTP = os.environ.get('TESTING', 'False') == 'True' or DEBUG

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    'rest_framework',
    'drf_spectacular',
    
    'ledger.apps.LedgerConfig',
    'api.apps.ApiConfig',
    'workers.apps.WorkersConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME', 'doubleledge'),
        'USER': os.environ.get('DB_USER', 'postgres'),
        'PASSWORD': os.environ.get('DB_PASSWORD', 'postgres'),
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '5432'),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True
STATIC_URL = 'static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'api.authentication.TenantJWTAuthentication',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'api.throttling.TenantRateThrottle',
    ],
    'EXCEPTION_HANDLER': 'api.exceptions.custom_exception_handler',
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'DoubleLedge API',
    'DESCRIPTION': 'A double-entry accounting engine',
    'VERSION': '1.0.0',
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'json': {
            '()': 'pythonjsonlogger.jsonlogger.JsonFormatter',
            'fmt': '%(asctime)s %(levelname)s %(name)s %(message)s'
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
}

# Celery Configuration
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'

from celery.schedules import crontab
CELERY_BEAT_SCHEDULE = {
    'run-ledger-reconciliation-hourly': {
        'task': 'workers.tasks.run_reconciliation_audit',
        'schedule': crontab(minute='0'),  # run every hour on the hour
    },
    'dispatch-webhooks-every-minute': {
        'task': 'workers.tasks.dispatch_webhooks',
        'schedule': crontab(minute='*'),  # run every minute
    },
    'prune-webhooks-daily': {
        'task': 'workers.tasks.prune_webhooks',
        'schedule': crontab(minute='0', hour='1'),  # run daily at 1 AM
    },
    'create-snapshots-every-minute': {
        'task': 'workers.tasks.create_account_balance_snapshots',
        'schedule': crontab(minute='*'),
    },
    'expire-holds-every-minute': {
        'task': 'workers.tasks.expire_holds',
        'schedule': crontab(minute='*'),
    },
    'rollup-shards-every-minute': {
        'task': 'workers.tasks.rollup_shards',
        'schedule': crontab(minute='*'),
    }
}
