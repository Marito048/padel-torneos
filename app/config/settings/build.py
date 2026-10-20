"""Ajustes para la fase de build (collectstatic) en Railway.

El build no necesita ni la clave secreta ni la base de datos: sólo tiene que
recolectar los estáticos. Con estos valores el build no depende de que
``SECRET_KEY`` o ``DATABASE_URL`` estén definidos en el servicio, así que
``nixpacks.toml`` puede correr collectstatic antes de que se los configure.

``SECRET_KEY`` y ``DATABASE_URL`` de acá son de mentira: valen sólo para
generar el manifiesto de estáticos. El runtime usa ``config.settings.railway``.
"""

import os

# Se definen antes de importar base porque base.py los lee al importarse.
os.environ.setdefault("SECRET_KEY", "django-insecure-solo-para-el-build-no-usar-en-produccion")
os.environ.setdefault("DATABASE_URL", "sqlite:///build.sqlite3")

from .base import *  # noqa: E402,F401,F403
