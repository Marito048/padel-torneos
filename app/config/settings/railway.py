"""Ajustes para Railway.

Hereda todo lo de ``prod`` (DEBUG=False, SSL, HSTS, cookies seguras) y le
suma lo que Railway necesita:

* El dominio público del servicio lo inyecta Railway en
  ``RAILWAY_PUBLIC_DOMAIN``. Sin agregarlo a ``ALLOWED_HOSTS`` Django
  responde 400 "DisallowedHost" a todas las peticiones.
* El mismo dominio tiene que estar en ``CSRF_TRUSTED_ORIGINS``; si no, los
  formularios (login, turnos, etc.) fallan con 403 en Django 4+.
"""

import os

from django.core.exceptions import ImproperlyConfigured

from .prod import *  # noqa: F401,F403

# En Railway la base tiene que ser SIEMPRE el Postgres del plugin. Si
# faltara DATABASE_URL, base.py caeria a SQLite, que en Railway es efimero:
# la app arrancaria igual (y el deploy diria "Completed") pero guardando los
# datos en un disco que se pierde en cada redeploy. Mejor fallar aca.
if not os.environ.get("DATABASE_URL", "").strip():
    raise ImproperlyConfigured(
        "Falta DATABASE_URL. Agregala como variable de REFERENCIA en el servicio "
        "padel-torneos (Settings > Variables > New Variable > Add Reference): "
        "DATABASE_URL = ${{Postgres.DATABASE_URL}}. "
        "Sin esto la app usaria SQLite, que en Railway es efimero."
    )

# Railway inyecta RAILWAY_PUBLIC_DOMAIN (el principal) y
# RAILWAY_STATIC_DOMAINS (lista separada por comas de todos los dominios
# .up.railway.app del servicio). Se suman todos a ALLOWED_HOSTS y a
# CSRF_TRUSTED_ORIGINS.
_RAILWAY_DOMAINS = []
for _var in ("RAILWAY_PUBLIC_DOMAIN", "RAILWAY_STATIC_DOMAINS"):
    for _domain in os.environ.get(_var, "").split(","):
        _domain = _domain.strip()
        if _domain and _domain not in _RAILWAY_DOMAINS:
            _RAILWAY_DOMAINS.append(_domain)

_hosts = [h for h in (ALLOWED_HOSTS or []) if h]
_origins = [o for o in (CSRF_TRUSTED_ORIGINS or []) if o]

for _domain in _RAILWAY_DOMAINS:
    if _domain not in _hosts:
        _hosts.append(_domain)
    _origin = f"https://{_domain}"
    if _origin not in _origins:
        _origins.append(_origin)

if not _hosts:
    # Sin RAILWAY_PUBLIC_DOMAIN y sin ALLOWED_HOSTS configurados se acepta
    # cualquier Host: es preferible un servicio accesible a uno que rechace
    # todas las peticiones. Con el dominio de Railway presente queda acotado.
    _hosts = ["*"]

ALLOWED_HOSTS = _hosts
CSRF_TRUSTED_ORIGINS = _origins

# Railway sirve todo el tráfico por HTTPS detrás de su proxy; prod.py ya
# activa SECURE_SSL_REDIRECT y base.py define SECURE_PROXY_SSL_HEADER.
