from .base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ["*"]
# Orígenes locales de runserver + los que lleguen por .env (ngrok, Tailscale Funnel, etc.).
CSRF_TRUSTED_ORIGINS = list(
    dict.fromkeys(
        [
            "http://localhost",
            "http://127.0.0.1",
            "http://localhost:8000",
            *CSRF_TRUSTED_ORIGINS,
        ]
    )
)
# En dev se usa runserver / gunicorn sin TLS.
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Sin manifiesto con hash en dev: permite tests y cambios de estáticos sin
# volver a ejecutar collectstatic.
STORAGES = {
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}
WHITENOISE_USE_FINDERS = True
