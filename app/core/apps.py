import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "Gestión de torneos (core)"

    def ready(self):
        # La progresión automática de llaves vive en ``Match.save`` y en
        # ``core.services.tournament_workflow``. No se usan señales para evitar
        # carreras y excepciones silenciadas: cualquier fallo se propaga al
        # usuario y revierte la transacción que guardó el resultado.
        return
