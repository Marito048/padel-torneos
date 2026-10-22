from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403

if not DATABASE_URL:
    # En desarrollo se permite SQLite; en producción sería una base efímera
    # (o vacía) mientras la app parece funcionar. Mejor fallar al arrancar.
    raise ImproperlyConfigured(
        "Falta DATABASE_URL en producción. Definí la URL de PostgreSQL antes "
        "de iniciar la aplicación."
    )

DEBUG = False
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
# Primera versión del piloto: los pagos online están deshabilitados incluso si
# una variable de entorno intenta activarlos. Para la segunda etapa hay que
# retirar esta línea, habilitar el interruptor, cargar credenciales y repetir
# las pruebas E2E antes de desplegar.
MERCADO_PAGO_ONLINE_PAYMENTS_ENABLED = False
# En producción no se acepta cobrar online sin verificar la firma del webhook.
# La variable de entorno puede activarlo en desarrollo, pero no desactivarlo
# en este módulo.
MERCADO_PAGO_REQUIRE_WEBHOOK_SECRET = True

# Configurar logs a stdout ya está en base.
