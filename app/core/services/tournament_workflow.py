"""Workflow transaccional para resultados, walkovers y correcciones.

Reglas de bloqueo:
- Un partido de zona no puede corregirse si la llave ya tiene datos deportivos
  reales (resultado, walkover o ganador de un partido real).
- Un partido eliminatorio no puede corregirse si alguno de sus partidos
  dependientes (los que reciben a su ganador) tiene datos reales.
- Los descansos (byes) son estructura, no datos: no bloquean correcciones.
- Nada se elimina ni sobrescribe para forzar un cambio.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ValidationError
from django.db import transaction

from core.models import Match, MatchConflictError, Tournament, flip_score, implied_winner_id
from core.services.draws import (
    PLAYOFF_STAGES,
    BracketConflictError,
    ensure_playoff_bracket,
    is_zone_complete,
    sync_bracket_progression,
)

logger = logging.getLogger(__name__)


def _dependency_matches(match: Match) -> list[Match]:
    """Partidos que dependen de este resultado, siguiendo los enlaces reales."""
    if match.stage == "group":
        return list(Match.objects.filter(tournament=match.tournament, stage__in=PLAYOFF_STAGES))
    if not match.next_match_id:
        return []
    frontier = [Match.objects.get(pk=match.next_match_id)]
    dependents: list[Match] = []
    seen: set[int] = set()
    while frontier:
        current = frontier.pop()
        if current.pk in seen:
            continue
        seen.add(current.pk)
        dependents.append(current)
        if current.next_match_id:
            try:
                frontier.append(Match.objects.get(pk=current.next_match_id))
            except Match.DoesNotExist:
                logger.warning("Enlace de llave roto: partido %s apunta a %s inexistente.", current.pk, current.next_match_id)
    return dependents


def can_update_match(match: Match, changes: dict | None = None) -> bool:
    match = Match.objects.select_related("tournament").get(pk=match.pk)
    if changes:
        changed = False
        for key, value in changes.items():
            old = getattr(match, key, None)
            if hasattr(value, "pk"):
                value = value.pk
            if hasattr(old, "pk"):
                old = old.pk
            if old != value:
                changed = True
                break
        if not changed:
            return True
    for dep in _dependency_matches(match):
        if dep.has_real_data:
            return False
    return True


def register_match_result(
    match: Match,
    *,
    score: str | None = None,
    winner=None,
    is_walkover: bool | None = None,
    walkover_reason: str | None = None,
    court=None,
    played_at=None,
    actor=None,
    expected_version: int | None = None,
) -> Match:
    """Registra o corrige un resultado de forma atómica y serializada.

    Toma el lock del torneo y del partido en ese orden, re-lee el estado dentro
    del lock y valida ``expected_version`` si el formulario la envió. Si la
    progresión falla, toda la operación se revierte.
    """
    try:
        initial = Match.objects.only("pk", "tournament_id").get(pk=match.pk)
    except Match.DoesNotExist as exc:
        raise MatchConflictError("El partido ya no existe. Actualizá la página.") from exc

    with transaction.atomic():
        try:
            locked_tournament = Tournament.objects.select_for_update().get(
                pk=initial.tournament_id
            )
        except Tournament.DoesNotExist as exc:
            raise MatchConflictError(
                "El torneo fue eliminado por otra operación. Actualizá la página."
            ) from exc
        try:
            match = (
                Match.objects.select_for_update()
                .get(pk=match.pk)
            )
        except Match.DoesNotExist as exc:
            raise MatchConflictError(
                "El partido fue eliminado por otra operación. Actualizá la página."
            ) from exc
        match.tournament = locked_tournament

        if expected_version is not None:
            try:
                expected = int(expected_version)
            except (TypeError, ValueError):
                expected = None
            if expected is not None and match.version != expected:
                raise MatchConflictError(
                    "El partido fue modificado por otra persona mientras cargabas el "
                    "resultado. Actualizá la página para ver el estado actual."
                )

        old_score = match.score
        old_winner_id = match.winner_id
        old_walkover = match.is_walkover
        old_reason = match.walkover_reason

        new_score = match.score if score is None else score
        new_winner = match.winner if winner is None else winner
        new_walkover = match.is_walkover if is_walkover is None else is_walkover
        new_reason = match.walkover_reason if walkover_reason is None else walkover_reason
        new_winner_pk = getattr(new_winner, "pk", new_winner)

        # El ganador elegido manda: si el marcador vino escrito desde la otra
        # pareja (p. ej. 6-3 6-2 eligiendo ganadora a la segunda), se acomoda y
        # no se rechaza.
        if winner is not None and not new_walkover and (new_score or "").strip():
            if match.team1_id and match.team2_id:
                implied_id = implied_winner_id(
                    new_score, match.team1_id, match.team2_id
                )
                if implied_id is not None and implied_id != new_winner_pk:
                    new_score = flip_score(new_score)

        changes = not (
            (new_score or "") == (old_score or "")
            and new_winner_pk == old_winner_id
            and bool(new_walkover) == bool(old_walkover)
            and (new_reason or "") == (old_reason or "")
        )

        if locked_tournament.status in ("draft", "open"):
            Tournament.objects.filter(pk=locked_tournament.pk).update(status="running")
            locked_tournament.status = "running"

        if changes:
            for dep in _dependency_matches(match):
                if dep.has_real_data:
                    raise ValidationError(
                        "Existe un partido dependiente con resultado, walkover o datos registrados. "
                        "Revisá y corregí primero las etapas posteriores."
                    )

        if score is not None:
            match.score = new_score
        if winner is not None:
            match.winner = winner
        if is_walkover is not None:
            match.is_walkover = is_walkover
        if walkover_reason is not None:
            match.walkover_reason = walkover_reason
        if court is not None:
            match.court = court
        if played_at is not None:
            match.played_at = played_at
        if winner is None and not match.is_walkover and (match.score or "").strip():
            match.derive_winner_from_score()
        if actor is not None and (match.winner_id is not None or match.is_walkover):
            from django.utils import timezone as tz

            match.recorded_by = actor
            match.recorded_at = tz.now()

        match.full_clean()
        match._skip_auto_progress = True
        match.save()

        # Progresión dentro de la misma transacción: si falla, se revierte todo.
        if match.stage == "group":
            from core.services.progression import refresh_tournament_structure

            refresh_tournament_structure(locked_tournament.pk)
        elif match.stage in PLAYOFF_STAGES:
            sync_bracket_progression(locked_tournament)

    return Match.objects.get(pk=match.pk)


def safe_correction(match: Match, *, new_score=None, new_winner=None, new_is_walkover=None, new_walkover_reason=None, actor=None) -> Match:
    changes = {}
    if new_score is not None:
        changes["score"] = new_score
    if new_winner is not None:
        changes["winner"] = new_winner
    if new_is_walkover is not None:
        changes["is_walkover"] = new_is_walkover
    if new_walkover_reason is not None:
        changes["walkover_reason"] = new_walkover_reason
    if not can_update_match(match, changes):
        raise ValidationError(
            "La corrección está bloqueada por dependencias con resultado, datos parciales o inconsistentes."
        )
    return register_match_result(
        match,
        score=new_score,
        winner=new_winner,
        is_walkover=new_is_walkover,
        walkover_reason=new_walkover_reason,
        actor=actor,
    )


__all__ = [
    "register_match_result",
    "safe_correction",
    "can_update_match",
    "BracketConflictError",
    "MatchConflictError",
]
