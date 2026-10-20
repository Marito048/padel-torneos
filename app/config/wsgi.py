import os
from django.core.wsgi import get_wsgi_application

# OJO: setdefault no sirve acá. Si DJANGO_SETTINGS_MODULE existe pero vacía
# (pasa cuando el build no inyecta la variable), setdefault no la pisa y
# Django falla con "settings are not configured".
if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.prod"

application = get_wsgi_application()
