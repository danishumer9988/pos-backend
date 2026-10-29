import os
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """Tiny .env loader so you don't need an extra package. Returns the names it read.
    Handles the Windows quirks: a hidden BOM at the start of the file, CRLF line endings,
    `export NAME=value`, quotes, and trailing `# comments`."""
    if not path.exists():
        return []
    names = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in ("'", '"') and v.count(v[0]) >= 2:
            v = v[1:v.index(v[0], 1)]              # quoted: keep everything inside the quotes
        else:
            v = v.split(" #", 1)[0].strip()        # unquoted: drop a trailing " # comment"
        os.environ.setdefault(k.strip(), v)
        names.append(k.strip())
    return names


DOTENV_PATH = BASE_DIR / ".env"
DOTENV_KEYS = _load_dotenv(DOTENV_PATH)           # names only, shown by `manage.py test_email`

_DEV_KEY = "django-insecure-dev-key-change-me"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", _DEV_KEY)
# On Vercel (it sets VERCEL=1) debug is OFF unless you explicitly turn it on.
DEBUG = os.getenv("DJANGO_DEBUG", "False" if os.getenv("VERCEL") else "True").lower() == "true"

if not DEBUG:
    # Fail early (and say exactly why) instead of running a public site with a guessable key.
    from django.core.exceptions import ImproperlyConfigured
    _where = f" This deployment's Vercel environment is '{os.getenv('VERCEL_ENV')}'." if os.getenv("VERCEL_ENV") else ""
    if not os.getenv("DJANGO_SECRET_KEY", "").strip():
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is NOT SET." + _where + " Vercel: Project > Settings > Environment Variables > add "
            "DJANGO_SECRET_KEY, tick Production, Preview and Development, then Redeploy.")
    if "change" in SECRET_KEY.lower() or "insecure" in SECRET_KEY.lower():
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is a SAMPLE value (it contains 'change' or 'insecure'). Replace it with a real random key: "
            "python -c \"import secrets; print(secrets.token_urlsafe(60))\"")
    if len(SECRET_KEY) < 32:
        raise ImproperlyConfigured(f"DJANGO_SECRET_KEY is only {len(SECRET_KEY)} characters long; it needs at least 32.")


def _origins(raw):
    """Website addresses typed into a dashboard field: forgive spaces, a trailing / and a missing https://."""
    out = []
    for x in raw.split(","):
        x = x.strip().rstrip("/")
        if x:
            out.append(x if "://" in x else f"https://{x}")
    return out

ALLOWED_HOSTS = [x.strip() for x in os.getenv(
    "ALLOWED_HOSTS", "127.0.0.1,localhost"
).split(",") if x.strip()]
# Vercel provides the deployment's own hostnames; allow them automatically.
ALLOWED_HOSTS += [
    h for h in (
        os.getenv("VERCEL_URL"),
        os.getenv("VERCEL_BRANCH_URL"),
        os.getenv("VERCEL_PROJECT_PRODUCTION_URL")
    ) if h
]
if os.getenv("VERCEL"):
    ALLOWED_HOSTS.append(".vercel.app")
# Needed for the /admin login when the backend is served on https from another domain.
CSRF_TRUSTED_ORIGINS = _origins(os.getenv("CSRF_TRUSTED_ORIGINS", ""))

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "pos",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",   # serves /admin CSS on hosts without a CDN (Render, Railway...)
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        "DATABASE_URL is NOT SET. This project uses PostgreSQL only (no SQLite). "
        "Set DATABASE_URL to your Neon/Postgres connection string "
        "(Vercel: Project > Settings > Environment Variables; locally: put it in backend/.env).")

u = urlparse(DATABASE_URL)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(u.path.lstrip("/")),
        "USER": unquote(u.username or ""),
        "PASSWORD": unquote(u.password or ""),   # passwords with @ : / % arrive percent-encoded
        "HOST": u.hostname or "",
        "PORT": str(u.port or 5432),
        "OPTIONS": {"sslmode": os.getenv("DB_SSLMODE", "require")},
        "CONN_MAX_AGE": 0,                      # serverless: don't keep connections between requests
        "DISABLE_SERVER_SIDE_CURSORS": True,    # required by pooled connections (Neon "-pooler" URLs)
    }
}
# Serverless instances don't share memory, so rate limits (login/OTP) need a shared store.
# Create its table once with:  python manage.py createcachetable
CACHES = {"default": {"BACKEND": "pos.cache.SafeDatabaseCache", "LOCATION": "django_cache"}}

AUTH_USER_MODEL = "pos.User"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "pos.authentication.BearerTokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "login": "10/min",
        "signup": "20/hour",
        "otp": "20/hour",
        "demo": "10/hour",
    },
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
}

CORS_ALLOWED_ORIGINS = _origins(os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000"))
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_ALL_ORIGINS = DEBUG  # dev convenience: any localhost port works

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Karachi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Email -------------------------------------------------------------------------
# If EMAIL_HOST is set we send real emails over SMTP. With no EMAIL_HOST the code is only
# printed in the backend terminal (fine for local development). An explicit EMAIL_BACKEND wins.
EMAIL_HOST = os.getenv("EMAIL_HOST", "").strip()
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587") or 587)
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND") or (
    "django.core.mail.backends.smtp.EmailBackend" if EMAIL_HOST
    else "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "").strip()
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
if "gmail" in EMAIL_HOST.lower():
    EMAIL_HOST_PASSWORD = EMAIL_HOST_PASSWORD.replace(" ", "")   # Google shows app passwords as 4 groups with spaces
# Port 465 = SSL from the first byte; port 587 = STARTTLS. Using the wrong one makes the connection hang or fail.
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "True" if EMAIL_PORT == 465 else "False").lower() == "true"
EMAIL_USE_TLS = (not EMAIL_USE_SSL) and os.getenv("EMAIL_USE_TLS", "True").lower() == "true"
# Mail providers reject unknown senders, so default the "From" to the login address.
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL") or EMAIL_HOST_USER or "no-reply@example.com"
SERVER_EMAIL = DEFAULT_FROM_EMAIL


# Used in the "your POS is ready" email and for the optional new-signup alert.
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
ADMIN_NOTIFY_EMAIL = os.getenv("ADMIN_NOTIFY_EMAIL", "")


EMAIL_TIMEOUT = 10  # never let a slow mail server hang a signup request

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")  # we sit behind the host's https proxy
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# Without this, errors on a production host (DEBUG off) are not written anywhere you can read them.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
    "loggers": {"django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False}},
}