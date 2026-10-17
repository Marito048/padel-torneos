"""Progresión automática de la llave eliminatoria.

Se ejecuta después de guardar un partido con resultado real (o un descanso).
- Si el partido es de zona: genera/limpia la llave según la completitud de zonas.
- Si es eliminatorio: coloca al ganador en el partido siguiente.

Nunca oculta errores: los conflictos lanzan ``BracketConflictError`` y se
propagan al usuario dentro de la misma transacción.
"""

from __future__ import annotations

import logging

from django.db import transaction

from core.models import Match, Tournament
from core.services.draws import (
    PLAYOFF_STAGES,
    BracketConflictError,
    ensure_playoff_bracket,
    is_zone_complete,
    sync_bracket_progression,
)

logger = logging.getLogger(__name__)


def _has_real_data_in_bracket(tournament: Tournament) -> bool:
    return any(
        row.has_real_data
        for row in Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES)
    )


def progress_after_match_save(match_id: int) -> None:
    """Punto de entrada invocado por ``Match.save`` (una sola vez por guardado)."""
    match = Match.objects.select_related("tournament").get(pk=match_id)
    tournament = match.tournament
    if not tournament.playoffs_enabled:
        return
    if match.stage == "group":
        _progress_group_result(tournament)
    elif match.stage in PLAYOFF_STAGES:
        try:
            sync_bracket_progression(tournament)
        except BracketConflictError:
            logger.exception("Conflicto al avanzar el ganador en la llave (torneo %s).", tournament.pk)
            raise


def refresh_tournament_structure(tournament_id: int) -> None:
    """Recalcula llave/estructura tras cambios sin resultado (crear o borrar partidos)."""
    tournament = Tournament.objects.get(pk=tournament_id)
    if tournament.playoffs_enabled:
        _progress_group_result(tournament)


def _progress_group_result(tournament: Tournament) -> None:
    with transaction.atomic():
        locked = Tournament.objects.select_for_update().get(pk=tournament.pk)
        zones = list(locked.zones.all())
        if not zones:
            return
        complete = all(is_zone_complete(zone) for zone in zones)
        if complete:
            qualifiers_total = len(zones) * max(1, locked.qualifiers_per_zone)
            if qualifiers_total < 2:
                return
            try:
                ensure_playoff_bracket(locked)
            except BracketConflictError:
                logger.exception("Conflicto al regenerar la llave tras resultado de zonas (torneo %s).", locked.pk)
                raise
            return

        # Las zonas dejaron de estar completas (por ejemplo, una corrección).
        # Sólo se limpia una llave sin datos deportivos reales.
        qs = Match.objects.filter(tournament=locked, stage__in=PLAYOFF_STAGES)
        if qs.exists() and not _has_real_data_in_bracket(locked):
            qs.delete()
