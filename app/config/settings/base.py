from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
)
# Lee .env desde la raiz del proyecto (padel-torneos/.env). En docker se pasa por env_file.
environ.Env.read_env(BASE_DIR.parent / ".env", overwrite=False)

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    # Autenticación social (Google) con django-allauth.
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "core",
]

SITE_ID = 1

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.auth_extras",
            ],
        },
    },
]

DATABASES = {"default": env.db("DATABASE_URL")}

# Contraseñas: sólo 8 caracteres como mínimo (sin exigir mayúsculas, números
# ni símbolos): es una app de club, la idea es que nadie se complique.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
]

LANGUAGE_CODE = "es-ar"
TIME_ZONE = "America/Argentina/Buenos_Aires"
USE_I18N = True
USE_TZ = True

LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "tournament_select"
LOGOUT_REDIRECT_URL = "account_login"

# ---------------------------------------------------------------------------
# django-allauth
# ---------------------------------------------------------------------------
ACCOUNT_LOGIN_METHODS = {"username"}
ACCOUNT_SIGNUP_FIELDS = ["username*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_ADAPTER = "core.adapters.PadelAccountAdapter"
SOCIALACCOUNT_AUTO_SIGNUP = False  # Los accesos se otorgan por complejo desde la administración.
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
# El botón "Ingresar con Google" va directo a Google, sin la pantalla
# intermedia de confirmación que muestra allauth por defecto.
SOCIALACCOUNT_LOGIN_ON_GET = True
# Invitaciones por email: el registro con Google se habilita sólo para los
# emails invitados desde Configuración general; el formulario pide nombre,
# apellido y usuario para completar la cuenta del invitado.
SOCIALACCOUNT_ADAPTER = "core.adapters.PadelSocialAccountAdapter"
SOCIALACCOUNT_FORMS = {"signup": "core.forms.PadelSocialSignupForm"}

# Inicio de sesión con Google: se activa sólo si hay credenciales OAuth cargadas.
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID", default="")
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET", default="")
GOOGLE_LOGIN_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
SOCIALACCOUNT_PROVIDERS = {}
if GOOGLE_LOGIN_ENABLED:
    SOCIALACCOUNT_PROVIDERS["google"] = {
        "APP": {
            "client_id": GOOGLE_CLIENT_ID,
            "secret": GOOGLE_CLIENT_SECRET,
            "key": "",
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    }

# ---------------------------------------------------------------------------
# Email saliente (aviso de cuenta creada con usuario y contraseña temporal)
# ---------------------------------------------------------------------------
# Sin SMTP configurado los mails se imprimen en la consola del servicio; con
# EMAIL_HOST cargado en .env se envían de verdad (ej. Gmail con contraseña de
# aplicación: smtp.gmail.com / 587 / usuario@gmail.com / contraseña).
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default=(
        "django.core.mail.backends.smtp.EmailBackend"
        if EMAIL_HOST
        else "django.core.mail.backends.console.EmailBackend"
    ),
)
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Pádel Server <no-reply@padel.local>")

# Aviso de nuevas solicitudes de registro (ver core/notifications.py): email
# extra (los administradores generales con email ya reciben siempre) y
# WhatsApp vía CallMeBot.
SIGNUP_ALERT_EMAIL = env("SIGNUP_ALERT_EMAIL", default="")
WHATSAPP_ALERT_PHONE = env("WHATSAPP_ALERT_PHONE", default="")
WHATSAPP_ALERT_APIKEY = env("WHATSAPP_ALERT_APIKEY", default="")

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
