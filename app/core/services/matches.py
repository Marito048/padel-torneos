"""Operaciones de partidos de zona (creación y borrado seguros)."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max, Q

from core.models import Match, Team, Tournament, Zone


def create_group_match(tournament: Tournament, zone: Zone, team1: Team, team2: Team, **extra) -> Match:
    if team1.pk == team2.pk:
        raise ValidationError("Los dos participantes deben ser duplas distintas.")
    with transaction.atomic():
        locked = Tournament.objects.select_for_update().get(pk=tournament.pk)
        try:
            zone = Zone.objects.select_for_update().get(pk=zone.pk)
        except Zone.DoesNotExist as exc:
            raise ValidationError("La zona ya no existe. Actualizá la página.") from exc
        if zone.tournament_id != locked.pk:
            raise ValidationError("La zona no pertenece al torneo seleccionado.")
        allowed = set(zone.teams.values_list("pk", flat=True))
        if team1.pk not in allowed or team2.pk not in allowed:
            raise ValidationError("Ambas duplas deben estar asignadas a la zona elegida.")
        existing = (
            Match.objects.filter(tournament=locked, stage="group", zone=zone)
            .filter(Q(team1=team1, team2=team2) | Q(team1=team2, team2=team1))
            .exists()
        )
        if existing:
            raise ValidationError("Ya existe un partido entre estas duplas en esta zona.")
        low, high = (team1, team2) if team1.pk < team2.pk else (team2, team1)
        current_max = (
            Match.objects.filter(tournament=locked, stage="group", zone=zone)
            .aggregate(max_slot=Max("slot"))
            .get("max_slot")
            or 0
        )
        match = Match(
            tournament=locked,
            stage="group",
            zone=zone,
            round_number=1,
            slot=current_max + 1,
            team1=low,
            team2=high,
            **extra,
        )
        try:
            match.save()
        except IntegrityError as exc:
            raise ValidationError("Ya existe un partido entre estas duplas en esta zona.") from exc
        return match


def delete_group_match(match: Match) -> None:
    if match.stage != "group":
        raise ValidationError("Los partidos de llave se administran automáticamente; no se borran a mano.")
    with transaction.atomic():
        # Mismo orden que el resto de las escrituras: torneo → partido.
        try:
            tournament = Tournament.objects.select_for_update().get(pk=match.tournament_id)
        except Tournament.DoesNotExist as exc:
            raise ValidationError("El torneo ya no existe.") from exc
        fresh = (
            Match.objects.select_for_update()
            .filter(pk=match.pk, tournament=tournament)
            .first()
        )
        if fresh is None:
            raise ValidationError("El partido ya no existe.")
        if fresh.stage != "group":
            raise ValidationError("Los partidos de llave se administran automáticamente; no se borran a mano.")
        if fresh.has_real_data:
            raise ValidationError("No se puede eliminar un partido con resultado o walkover.")
        fresh.delete()
        from core.services.progression import refresh_tournament_structure

        refresh_tournament_structure(tournament.pk)
