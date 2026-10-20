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

from .prod import *  # noqa: F401,F403

_RAILWAY_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()

_hosts = [h for h in (ALLOWED_HOSTS or []) if h]
_origins = [o for o in (CSRF_TRUSTED_ORIGINS or []) if o]

if _RAILWAY_DOMAIN:
    if _RAILWAY_DOMAIN not in _hosts:
        _hosts.append(_RAILWAY_DOMAIN)
    _origin = f"https://{_RAILWAY_DOMAIN}"
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
