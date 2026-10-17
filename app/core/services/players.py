"""Servicios de jugadores, duplas, inscripciones y zonas.

Todas las operaciones que pueden violar una regla deportiva se ejecutan en
transacciones y sincronizan ``TournamentPlayer``, cuya restricción única
(torneo, jugador) es la garantía a nivel de base de datos.
"""

from __future__ import annotations

import hashlib
import json
import random
import secrets
import unicodedata

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q

from core.models import (
    Match,
    Player,
    Registration,
    Team,
    TieBreakAudit,
    Tournament,
    TournamentPlayer,
    Zone,
    ZoneTeam,
)

# Estados que cuentan como «dentro del torneo». Las inscripciones pendientes
# (autoinscripción pública) recién entran cuando el complejo las confirma.
IN_TOURNAMENT_REGISTRATION_STATUSES = ["paid", "confirmed"]


def registration_teams(tournament) -> set[int]:
    return set(
        Registration.objects.filter(
            tournament=tournament, status__in=IN_TOURNAMENT_REGISTRATION_STATUSES
        ).values_list("team_id", flat=True)
    )


def zone_teams_ids(tournament) -> set[int]:
    return set(ZoneTeam.objects.filter(zone__tournament=tournament).values_list("team_id", flat=True))


def tournament_team_ids(tournament) -> set[int]:
    return registration_teams(tournament) | zone_teams_ids(tournament)


def tournament_teams(tournament):
    return Team.objects.filter(pk__in=tournament_team_ids(tournament)).select_related("player1", "player2")


def player_ids_in_tournament(tournament) -> set[int]:
    rows = set(TournamentPlayer.objects.filter(tournament=tournament).values_list("player_id", flat=True))
    if rows:
        return rows
    # Fallback defensivo si la sincronización aún no corrió (datos heredados).
    result: set[int] = set()
    for team in Team.objects.filter(pk__in=tournament_team_ids(tournament)).only("player1_id", "player2_id"):
        result.update(team.player_ids)
    return result


def available_players(tournament, exclude_team: Team | None = None):
    qs = Player.objects.filter(active=True).order_by("last_name", "first_name")
    used = player_ids_in_tournament(tournament)
    if exclude_team is not None:
        used -= exclude_team.player_ids
    return qs.exclude(pk__in=used)


# ---------------------------------------------------------------------------
# Registro de jugadores reutilizables (el panel busca/crea sobre la base global)
# ---------------------------------------------------------------------------


def normalize_name(value: str) -> str:
    """Nombre comparable: sin acentos, sin comas, minúsculas y espacios simples."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.replace(",", " ").lower().split())


def _player_name_forms(player: Player) -> set[str]:
    return {
        normalize_name(f"{player.first_name} {player.last_name}"),
        normalize_name(f"{player.last_name} {player.first_name}"),
    }


def find_player_by_name(raw: str) -> Player | None:
    """Devuelve el jugador existente cuyo nombre coincide con ``raw``.

    Tolera acentos, mayúsculas y orden («Pérez, Juan», «juan perez»). Si hay
    más de uno con el mismo nombre, prioriza al usado más recientemente en
    torneos y, si nunca jugó, al más antiguo.
    """
    target = normalize_name(raw)
    if not target:
        return None
    matches = [
        player
        for player in Player.objects.filter(active=True).only("id", "first_name", "last_name")
        if target in _player_name_forms(player)
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    by_id = {player.pk: player for player in matches}
    recent = (
        TournamentPlayer.objects.filter(player_id__in=by_id)
        .order_by("-id")
        .values_list("player_id", flat=True)
        .first()
    )
    if recent is not None and recent in by_id:
        return by_id[recent]
    return min(matches, key=lambda player: player.pk)


def parse_player_name(raw: str) -> tuple[str, str]:
    """Interpreta «Apellido, Nombre» o «Nombre Apellido…» devolviendo (nombre, apellido)."""
    text = " ".join((raw or "").split())
    if not text:
        raise ValidationError("Escribí el nombre del jugador.")
    if "," in text:
        last, _, first = text.partition(",")
        first, last = first.strip(), last.strip()
        if not first or not last:
            raise ValidationError(
                f"No pudimos interpretar «{raw}». Usá «Apellido, Nombre» o «Nombre Apellido»."
            )
        return first, last
    parts = text.split(" ")
    if len(parts) < 2:
        raise ValidationError(
            f"Para «{text}» hace falta apellido y nombre (por ejemplo «{text} García»)."
        )
    return parts[0], " ".join(parts[1:])


def resolve_player(raw: str, *, gender: str = "", level: str = "") -> tuple[Player, bool]:
    """Devuelve (jugador, creado).

    Si el nombre ya existe en la base, lo reutiliza (así no hay que volver a
    cargarlo). Si no existe, crea el jugador y queda guardado para los
    próximos torneos.
    """
    existing = find_player_by_name(raw)
    if existing is not None:
        return existing, False
    first, last = parse_player_name(raw)
    gender_value = gender if gender in dict(Player.GENDER) else "M"
    level_value = level if level in dict(Player.LEVEL) else "principiante"
    player = Player.objects.create(
        first_name=first,
        last_name=last,
        gender=gender_value,
        level=level_value,
    )
    return player, True


def sync_tournament_players(tournament) -> int:
    """Recalcula las entradas (torneo, jugador) y hace cumplir la unicidad."""
    with transaction.atomic():
        team_ids = tournament_team_ids(tournament)
        desired: dict[int, int] = {}
        for team in Team.objects.filter(pk__in=team_ids).only("id", "player1_id", "player2_id"):
            for pid in team.player_ids:
                if pid in desired and desired[pid] != team.id:
                    raise ValidationError("Un jugador no puede integrar dos duplas del mismo torneo.")
                desired[pid] = team.id

        existing = {
            row.player_id: row
            for row in TournamentPlayer.objects.select_for_update().filter(tournament=tournament)
        }
        for player_id, team_id in desired.items():
            row = existing.get(player_id)
            if row is None:
                try:
                    TournamentPlayer.objects.create(tournament=tournament, player_id=player_id, team_id=team_id)
                except IntegrityError as exc:  # carrera entre dos peticiones
                    raise ValidationError(
                        "Otro usuario asignó a ese jugador mientras se guardaba. Actualizá y reintentá."
                    ) from exc
            elif row.team_id != team_id:
                row.team_id = team_id
                row.save(update_fields=["team"])
        stale = [pid for pid in existing if pid not in desired]
        if stale:
            TournamentPlayer.objects.filter(tournament=tournament, player_id__in=stale).delete()
        return len(desired)


def merge_players(source: Player, target: Player) -> dict:
    """Unifica dos fichas duplicadas del mismo jugador.

    Conserva ``target`` y elimina ``source`` moviendo duplas, inscripciones,
    posiciones en zonas, partidos y ganadores. Se bloquea si los jugadores
    integran la misma dupla o si ambos están anotados en el mismo torneo,
    porque la unión dejaría datos imposibles (un jugador dos veces en un
    torneo o una dupla contra sí misma).
    """
    if source.pk == target.pk:
        raise ValidationError("Elegí dos jugadores distintos para unificar.")
    with transaction.atomic():
        source = Player.objects.select_for_update().get(pk=source.pk)
        target = Player.objects.select_for_update().get(pk=target.pk)
        if Team.objects.filter(
            Q(player1=source, player2=target) | Q(player1=target, player2=source)
        ).exists():
            raise ValidationError(
                "No se puede unificar: los dos jugadores forman la misma dupla como pareja entre sí. Corregí esa dupla primero."
            )
        source_tournaments = set(
            TournamentPlayer.objects.filter(player=source).values_list("tournament_id", flat=True)
        )
        target_tournaments = set(
            TournamentPlayer.objects.filter(player=target).values_list("tournament_id", flat=True)
        )
        shared = source_tournaments & target_tournaments
        if shared:
            names = ", ".join(
                Tournament.objects.filter(pk__in=shared).order_by("name").values_list("name", flat=True)
            )
            raise ValidationError(
                "No se puede unificar: ambos jugadores están anotados en el mismo torneo "
                f"({names}). Corregí primero una de las dos duplas de ese torneo."
            )

        stats = {"teams": 0, "registrations": 0, "zone_teams": 0, "matches": 0, "winners": 0}
        teams = list(
            Team.objects.select_related("player1", "player2").filter(Q(player1=source) | Q(player2=source))
        )
        team_ids = [team.pk for team in teams]
        affected = set(source_tournaments)
        affected |= set(
            Registration.objects.filter(team_id__in=team_ids).values_list("tournament_id", flat=True)
        )
        affected |= set(
            ZoneTeam.objects.filter(team_id__in=team_ids).values_list("zone__tournament_id", flat=True)
        )
        affected |= set(
            Match.objects.filter(Q(team1_id__in=team_ids) | Q(team2_id__in=team_ids)).values_list(
                "tournament_id", flat=True
            )
        )
        for team in teams:
            partner_id = team.player2_id if team.player1_id == source.pk else team.player1_id
            twin = (
                Team.objects.filter(
                    Q(player1_id=target.pk, player2_id=partner_id)
                    | Q(player1_id=partner_id, player2_id=target.pk)
                )
                .exclude(pk=team.pk)
                .first()
            )
            if twin is None:
                if team.player1_id == source.pk:
                    team.player1 = target
                else:
                    team.player2 = target
                team.save()
                stats["teams"] += 1
                continue
            in_use = (
                Registration.objects.filter(team=team).exists()
                or ZoneTeam.objects.filter(team=team).exists()
                or Match.objects.filter(Q(team1=team) | Q(team2=team)).exists()
                or TournamentPlayer.objects.filter(team=team).exists()
            )
            if not in_use:
                team.delete()
                stats["teams"] += 1
                continue
            # La dupla destino ya existe: se drena hacia ella todo lo que
            # referencia a la dupla duplicada y recién ahí se elimina.
            stats["teams"] += 1
            stats["registrations"] += Registration.objects.filter(team=team).update(team=twin)
            stats["zone_teams"] += ZoneTeam.objects.filter(team=team).update(team=twin)
            TournamentPlayer.objects.filter(team=team).update(team=twin)
            stats["matches"] += Match.objects.filter(team1=team).update(team1=twin)
            stats["matches"] += Match.objects.filter(team2=team).update(team2=twin)
            stats["winners"] += Match.objects.filter(winner=team).update(winner=twin)
            team.delete()

        TournamentPlayer.objects.filter(player=source).update(player=target)
        source.delete()
        for tournament_id in sorted(affected):
            tournament = Tournament.objects.filter(pk=tournament_id).first()
            if tournament is not None:
                sync_tournament_players(tournament)
        return stats


def delete_player(player: Player) -> dict:
    """Elimina una ficha sin datos de competencia (devuelve cuántas duplas borró).

    Se permite solo si el jugador no está en ningún torneo ni tiene partidos:
    en esos casos sus datos se conservan y la ficha puede desactivarse.
    """
    with transaction.atomic():
        player = Player.objects.get(pk=player.pk)
        entry = (
            TournamentPlayer.objects.filter(player=player)
            .select_related("tournament")
            .order_by("-tournament__start_date", "tournament__name")
            .first()
        )
        if entry is not None:
            raise ValidationError(
                f"«{player}» está en el torneo «{entry.tournament.name}». "
                "Primero dá de baja su pareja desde la sección Parejas y volvé a intentar."
            )
        teams = list(Team.objects.filter(Q(player1=player) | Q(player2=player)))
        played = list(
            Match.objects.filter(Q(team1__in=teams) | Q(team2__in=teams))
            .select_related("tournament")
            .order_by("tournament__name")
        )
        if played:
            tournaments = ", ".join(sorted({f"«{match.tournament.name}»" for match in played}))
            raise ValidationError(
                f"«{player}» tiene partidos en {tournaments}. "
                "No se puede eliminar la ficha sin alterar esos torneos; podés desactivarla."
            )
        Team.objects.filter(pk__in=[team.pk for team in teams]).delete()
        player.delete()
        return {"teams": len(teams)}


CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8


def _new_registration_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def ensure_registration_code(tournament: Tournament) -> str:
    """Devuelve el código de autoinscripción del torneo, generándolo si falta."""
    if tournament.registration_code:
        return tournament.registration_code
    tournament.registration_code = _new_registration_code()
    tournament.save(update_fields=["registration_code"])
    return tournament.registration_code


def regenerate_registration_code(tournament: Tournament) -> str:
    """Genera un código nuevo: el anterior deja de servir."""
    tournament.registration_code = _new_registration_code()
    tournament.save(update_fields=["registration_code"])
    return tournament.registration_code


def create_team(
    tournament: Tournament,
    player1: Player,
    player2: Player,
    name: str = "",
    *,
    registration_status: str = "confirmed",
    source: str = "panel",
) -> Team:
    """Crea (o reutiliza) la dupla y la inscribe en el torneo, validando unicidad.

    ``registration_status="pending"`` deja la inscripción como solicitud
    (autoinscripción pública): no cuenta como «dentro del torneo» hasta que el
    complejo la confirme.
    """
    if player1.pk == player2.pk:
        raise ValidationError("La dupla no puede tener el mismo jugador dos veces.")
    with transaction.atomic():
        locked = Tournament.objects.select_for_update().get(pk=tournament.pk)
        p1 = Player.objects.get(pk=player1.pk)
        p2 = Player.objects.get(pk=player2.pk)
        if not p1.active or not p2.active:
            raise ValidationError("Ambos jugadores deben estar activos.")
        used = player_ids_in_tournament(locked)
        conflicts = [p for p in (p1, p2) if p.pk in used]
        if conflicts:
            raise ValidationError(
                "Los siguientes jugadores ya integran otra dupla de este torneo: "
                + ", ".join(str(p) for p in conflicts)
            )
        team = Team.objects.filter(
            Q(player1=p1, player2=p2) | Q(player1=p2, player2=p1)
        ).first()
        default_name = f"{p1.last_name} - {p2.last_name}"
        if team is None:
            team = Team.objects.create(player1=p1, player2=p2, name=(name or "").strip() or default_name)
        elif not (team.name or "").strip():
            # Duplas viejas sin nombre: al reutilizarlas toman el nombre por defecto.
            team.name = default_name
            team.save(update_fields=["name"])
        existing = Registration.objects.filter(tournament=locked, team=team).exclude(status="cancelled").exists()
        if existing:
            raise ValidationError("Esa pareja ya está inscripta en el torneo.")
        previous = Registration.objects.filter(tournament=locked, team=team).first()
        if previous is not None:
            # Reinscripción de una dupla rechazada antes: se reactiva la misma fila.
            previous.status = registration_status
            previous.source = source
            previous.save()
        else:
            Registration.objects.create(
                tournament=locked, team=team, status=registration_status, source=source
            )
        sync_tournament_players(locked)
        return team


def release_team(tournament: Tournament, team: Team) -> None:
    """Da de baja la dupla del torneo liberando a sus jugadores."""
    with transaction.atomic():
        locked = Tournament.objects.select_for_update().get(pk=tournament.pk)
        team = Team.objects.get(pk=team.pk)
        if bracket_has_real_data(locked):
            raise ValidationError(
                "La llave ya tiene resultados cargados. No se puede dar de baja la dupla sin corregir primero la fase eliminatoria."
            )
        related_matches = Match.objects.filter(tournament=locked).filter(
            Q(team1=team) | Q(team2=team)
        )
        if any(m.has_real_data for m in related_matches):
            raise ValidationError(
                "La dupla tiene partidos con resultado o walkover. Corregí o eliminá esos partidos primero."
            )
        related_matches.delete()
        ZoneTeam.objects.filter(zone__tournament=locked, team=team).delete()
        Registration.objects.filter(tournament=locked, team=team).delete()
        sync_tournament_players(locked)
        from core.services.progression import refresh_tournament_structure

        refresh_tournament_structure(locked.pk)


def bracket_has_real_data(tournament: Tournament) -> bool:
    from core.services.draws import PLAYOFF_STAGES

    return any(
        match.has_real_data
        for match in Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES)
    )


def assign_team_to_zone(zone: Zone, team: Team) -> ZoneTeam:
    with transaction.atomic():
        tournament = Tournament.objects.select_for_update().get(pk=zone.tournament_id)
        if not Registration.objects.filter(tournament=tournament, team=team).exclude(status="cancelled").exists():
            raise ValidationError("La dupla no está inscripta en este torneo.")
        if ZoneTeam.objects.filter(zone__tournament=tournament, team=team).exists():
            raise ValidationError("La dupla ya está asignada a una zona de este torneo.")
        if bracket_has_real_data(tournament):
            raise ValidationError("La llave ya tiene resultados: no se puede modificar la composición de las zonas.")
        assignment = ZoneTeam.objects.create(zone=zone, team=team)
        ensure_zone_matches(zone)
        from core.services.progression import refresh_tournament_structure

        refresh_tournament_structure(tournament.pk)
        return assignment


def remove_team_from_zone(zone: Zone, team: Team) -> None:
    with transaction.atomic():
        if bracket_has_real_data(zone.tournament):
            raise ValidationError("La llave ya tiene resultados: no se puede modificar la composición de las zonas.")
        related = Match.objects.filter(tournament=zone.tournament, zone=zone).filter(
            Q(team1=team) | Q(team2=team)
        )
        if any(m.has_real_data for m in related):
            raise ValidationError("La dupla tiene partidos con resultado en esta zona. No se puede quitar.")
        related.delete()
        ZoneTeam.objects.filter(zone=zone, team=team).delete()
        from core.services.progression import refresh_tournament_structure

        refresh_tournament_structure(zone.tournament_id)


def ensure_zone_matches(zone: Zone) -> list[Match]:
    """Genera el round robin pendiente de una zona (idempotente)."""
    teams = list(zone.teams.order_by("pk"))
    existing = {
        tuple(sorted((m.team1_id, m.team2_id)))
        for m in Match.objects.filter(tournament=zone.tournament, stage="group", zone=zone)
        if m.team1_id and m.team2_id
    }
    created = []
    slot = Match.objects.filter(tournament=zone.tournament, stage="group", zone=zone).count()
    for i, team_a in enumerate(teams):
        for team_b in teams[i + 1:]:
            key = tuple(sorted((team_a.pk, team_b.pk)))
            if key in existing:
                continue
            slot += 1
            match = Match(
                tournament=zone.tournament,
                stage="group",
                zone=zone,
                round_number=1,
                slot=slot,
                team1=team_a if team_a.pk < team_b.pk else team_b,
                team2=team_b if team_a.pk < team_b.pk else team_a,
            )
            match.save()
            existing.add(key)
            created.append(match)
    return created


def _zone_draw_signature(team_ids: list[int]) -> str:
    payload = json.dumps(sorted(team_ids))
    return hashlib.sha256(payload.encode()).hexdigest()


def generate_zones(tournament: Tournament, actor=None) -> list[Zone]:
    """Distribuye las duplas inscriptas en zonas usando sorteo reproducible auditado."""
    with transaction.atomic():
        locked = Tournament.objects.select_for_update().get(pk=tournament.pk)
        teams = list(tournament_teams(locked))
        if len(teams) < 2:
            raise ValidationError("Necesitás al menos 2 duplas inscriptas para generar zonas.")
        if Match.objects.filter(tournament=locked, stage="group").filter(
            ~Q(score="") | Q(is_walkover=True) | Q(winner__isnull=False)
        ).exists():
            raise ValidationError("Ya hay resultados de zona cargados; no se puede regenerar el sorteo.")

        assigned = zone_teams_ids(locked)
        unassigned = [team for team in teams if team.pk not in assigned]
        size = max(2, locked.teams_per_zone)

        existing_zones = list(locked.zones.order_by("order", "name"))
        ordered_ids = [team.pk for team in unassigned]
        if ordered_ids:
            signature = _zone_draw_signature(ordered_ids)
            seed = secrets.randbits(63)
            rng = random.Random(seed)
            shuffled = list(ordered_ids)
            rng.shuffle(shuffled)
            TieBreakAudit.objects.create(
                tournament=locked,
                scope_type="zone_draw",
                scope_key=f"tournament:{locked.pk}:zones:v1",
                input_signature=signature,
                tiebreaker_seed=seed,
                participants_ordered=sorted(ordered_ids),
                result_ordered=shuffled,
                criteria_summary={"criteria": ["seeded_draw"], "zone_size": size},
                triggered_by=actor,
            )
        else:
            shuffled = []

        created: list[Zone] = []
        # 1) Completar zonas existentes con lugar disponible.
        for zone in existing_zones:
            capacity = size - zone.teams.count()
            while capacity > 0 and shuffled:
                team_id = shuffled.pop(0)
                ZoneTeam.objects.create(zone=zone, team_id=team_id)
                capacity -= 1
        # 2) Crear zonas nuevas evitando dejar una zona de una sola dupla.
        names_used = {zone.name for zone in existing_zones}
        order = max([zone.order for zone in existing_zones], default=0)
        while shuffled:
            take = size
            remaining_after = len(shuffled) - take
            if remaining_after == 1:
                take += 1
            take = min(take, len(shuffled))
            chunk = shuffled[:take]
            shuffled = shuffled[take:]
            order += 1
            letter = chr(ord("A") + (order - 1)) if order <= 26 else f"Z{order}"
            name = letter
            while name in names_used:
                order += 1
                name = chr(ord("A") + (order - 1)) if order <= 26 else f"Z{order}"
            names_used.add(name)
            zone = Zone.objects.create(tournament=locked, name=name, order=order)
            for team_id in chunk:
                ZoneTeam.objects.create(zone=zone, team_id=team_id)
            created.append(zone)

        for zone in locked.zones.all():
            ensure_zone_matches(zone)

        if locked.status in ("draft", "open"):
            locked.status = "running"
            locked.save(update_fields=["status"])
        return list(locked.zones.order_by("order", "name"))


def save_manual_zones(tournament: Tournament, selections: dict[int, str]) -> list[Zone]:
    """Rearma las zonas según la elección manual del organizador.

    ``selections`` mapea ``team_id`` → destino: ``""`` (sin zona), ``"z:<id>"``
    (zona existente) o ``"new:<n>"`` (zona nueva, numerada por orden de uso).
    Se aplican las mismas reglas que al sorteo automático: cada zona con duplas
    necesita entre 2 y ``teams_per_zone`` duplas, y las zonas que queden vacías
    se descartan. Una dupla con resultados cargados no se puede mover, y con la
    llave ya jugada no se toca la composición.
    """
    with transaction.atomic():
        locked = Tournament.objects.select_for_update().get(pk=tournament.pk)
        teams = {team.pk: team for team in tournament_teams(locked)}
        if not teams:
            raise ValidationError("Inscribí duplas antes de armar las zonas.")
        if bracket_has_real_data(locked):
            raise ValidationError(
                "La llave ya tiene resultados: no se puede modificar la composición de las zonas."
            )

        zones = {zone.pk: zone for zone in locked.zones.all()}
        current: dict[int, int] = {}
        for assignment in ZoneTeam.objects.filter(zone__tournament=locked):
            current.setdefault(assignment.team_id, assignment.zone_id)

        def parse(raw) -> int | str | None:
            value = str(raw or "").strip()
            if not value:
                return None
            if value.startswith("z:") and value[2:].isdigit() and int(value[2:]) in zones:
                return int(value[2:])
            if value.startswith("new:") and value[4:].isdigit() and int(value[4:]) >= 1:
                return f"new:{int(value[4:])}"
            raise ValidationError("La selección de zonas no es válida.")

        parsed: dict[int, int | str | None] = {}
        for team_id in teams:
            parsed[team_id] = current.get(team_id)
            if team_id in selections:
                parsed[team_id] = parse(selections[team_id])

        # Zonas vacías que nadie va a usar: se liberan ya para reusar nombre y orden.
        referenced = {value for value in parsed.values() if isinstance(value, int)}
        for zone in list(zones.values()):
            if zone.pk not in referenced and not zone.teams.exists():
                zone.delete()
                del zones[zone.pk]

        # Zonas nuevas (se numeran con las letras que siguen).
        new_slots = sorted({int(value[4:]) for value in parsed.values() if isinstance(value, str)})
        next_order = max([zone.order for zone in zones.values()], default=0)
        used_names = {zone.name for zone in zones.values()}
        new_zones: dict[int, Zone] = {}
        for slot in new_slots:
            while True:
                next_order += 1
                letter = chr(ord("A") + (next_order - 1)) if next_order <= 26 else f"Z{next_order}"
                if letter not in used_names:
                    break
            used_names.add(letter)
            new_zones[slot] = Zone.objects.create(tournament=locked, name=letter, order=next_order)

        target: dict[int, int | None] = {
            team_id: (new_zones[int(value[4:])].pk if isinstance(value, str) else value)
            for team_id, value in parsed.items()
        }

        names = {zone_id: zone.name for zone_id, zone in zones.items()}
        names.update({zone.pk: zone.name for zone in new_zones.values()})
        counts: dict[int, int] = {}
        for zone_id in target.values():
            if zone_id is not None:
                counts[zone_id] = counts.get(zone_id, 0) + 1
        max_size = max(2, locked.teams_per_zone)
        for zone_id, count in counts.items():
            if count < 2:
                raise ValidationError(
                    f"La zona {names[zone_id]} quedaría con una sola dupla: necesita al menos 2."
                )
            if count > max_size:
                raise ValidationError(
                    f"La zona {names[zone_id]} quedaría con {count} duplas y el máximo configurado es {max_size}."
                )

        # Aplicar los movimientos: primero liberar la dupla, después reasignarla.
        changed = [team_id for team_id in teams if current.get(team_id) != target[team_id]]
        for team_id in changed:
            related = Match.objects.filter(tournament=locked, stage="group").filter(
                Q(team1_id=team_id) | Q(team2_id=team_id)
            )
            if any(match.has_real_data for match in related):
                raise ValidationError(
                    f"La dupla {teams[team_id]} ya tiene resultados cargados en su zona; "
                    "corregí o eliminá esos partidos antes de moverla."
                )
            related.delete()
            ZoneTeam.objects.filter(zone__tournament=locked, team_id=team_id).delete()
        for team_id in changed:
            if target[team_id] is not None:
                ZoneTeam.objects.create(zone_id=target[team_id], team_id=team_id)

        # Segunda pasada: zonas que quedaron sin duplas tras los movimientos.
        for zone in list(locked.zones.all()):
            if not zone.teams.exists():
                zone.delete()

        for zone in locked.zones.all():
            ensure_zone_matches(zone)
        from core.services.progression import refresh_tournament_structure

        refresh_tournament_structure(locked.pk)
        if locked.status in ("draft", "open"):
            locked.status = "running"
            locked.save(update_fields=["status"])
        return list(locked.zones.order_by("order", "name"))
