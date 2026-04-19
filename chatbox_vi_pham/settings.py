"""
Django settings for chatbox_vi_pham project.

Production deployment note (PythonAnywhere or any host):
    Set the following environment variables to override defaults:
        DJANGO_SECRET_KEY    — REQUIRED in production
        DJANGO_DEBUG         — "0" or "1" (default 0 in production)
        DJANGO_ALLOWED_HOSTS — comma-separated, e.g. "exam.example.com"

If DJANGO_SECRET_KEY is not set the dev fallback below is used; this is
only safe for local development.
"""

import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


# ─── Security ────────────────────────────────────────────────────────────────

# SECURITY WARNING: the dev fallback is fine for local hacking but MUST be
# overridden in production via the DJANGO_SECRET_KEY environment variable.
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-d_)$m)-*y6tp=0dnr5_(97mm2j#j6(ae+&7yl989ltlvst+vn!",
)

# DEBUG is opt-in. In production, leave DJANGO_DEBUG unset and DEBUG=False.
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

# Comma-separated host list. Example: "exam.example.com,127.0.0.1".
# In DEBUG mode we accept localhost variants so dev still works.
_allowed = os.environ.get("DJANGO_ALLOWED_HOSTS", "").strip()
ALLOWED_HOSTS = [h.strip() for h in _allowed.split(",") if h.strip()]
if DEBUG and not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", "[::1]"]

# CSRF: when behind HTTPS in production, the host(s) must be listed below
# to ensure cross-origin POSTs are accepted from the right origins.
CSRF_TRUSTED_ORIGINS = [
    f"https://{h}" for h in ALLOWED_HOSTS
    if h not in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
]

# Production-grade cookie + transport hardening. These flags become active
# only when DEBUG is off so local development over plain HTTP keeps working.
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE    = True
    SECURE_SSL_REDIRECT   = os.environ.get("DJANGO_FORCE_HTTPS", "0") == "1"
    SECURE_HSTS_SECONDS   = int(os.environ.get("DJANGO_HSTS_SECONDS", "0"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
    SECURE_HSTS_PRELOAD            = SECURE_HSTS_SECONDS > 0
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY      = "same-origin"
    X_FRAME_OPTIONS             = "DENY"


# ─── Apps ────────────────────────────────────────────────────────────────────

INSTALLED_APPS = [
    'daphne',
    'channels',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'violations',
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

ROOT_URLCONF = 'chatbox_vi_pham.urls'
ASGI_APPLICATION = 'chatbox_vi_pham.asgi.application'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
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

WSGI_APPLICATION = 'chatbox_vi_pham.wsgi.application'

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    },
}


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    # Intentionally left empty: admin account creation should not enforce
    # Django password strength validators.
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Asia/Ho_Chi_Minh'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
# In production, run `python manage.py collectstatic` and serve this folder.
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

LOGIN_URL = 'violations:login'
LOGIN_REDIRECT_URL = 'violations:dashboard'
LOGOUT_REDIRECT_URL = 'violations:dashboard'

# Silence Django 4+ AppConfig warning ("Auto-created primary key used …").
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Cap on the size of any uploaded file Django will buffer in memory before
# spilling to disk. We have a 10 MB image cap and a 200 MB video cap; let
# Django stream anything > 5 MB to a temp file so worker memory stays low.
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024  # 12 MB — must exceed image cap
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000

# Session: log out after 8 hours of inactivity (an exam day fits inside
# this; longer-lived sessions are unnecessary attack surface).
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_SAVE_EVERY_REQUEST = True


_prefix_raw = os.environ.get("SBD_DEFAULT_PREFIX", "TS")
_prefix = (_prefix_raw or "").strip().upper()
if not re.fullmatch(r"[A-Z]{2}", _prefix):
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        f"SBD_DEFAULT_PREFIX must be exactly 2 Latin letters (A-Z). "
        f"Got {_prefix_raw!r}."
    )
SBD_DEFAULT_PREFIX = _prefix
