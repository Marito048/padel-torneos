"""Generación de cuadros y lógica deportiva del torneo.

Convenciones:
- Zonas round robin de N duplas. Clasifican ``qualifiers_per_zone`` duplas.
- Desempate: puntos, diferencia de sets, diferencia de games, sorteo auditable.
- Torneos al mejor de N sets; empate a 1 set se define con super tie-break.
- Llaves: árbol completo creado de una vez, con enlaces ``next_match``/``next_slot``.
  Los descansos (byes) se representan como partidos ``is_bye`` con la dupla real
  y sin rival, nunca con equipos ficticios.
"""

from __future__ import annotations

import hashlib
import json
import random
import secrets

from django.db import transaction
from django.db.models import Q

from core.models import Match, Team, TieBreakAudit, Tournament, Zone, ZoneTeam, classify_score

STAGE_ORDER = ["round_of_64", "round_of_32", "round_of_16", "quarter", "semi", "final"]
NEXT_STAGE = {
    "round_of_64": "round_of_32",
    "round_of_32": "round_of_16",
    "round_of_16": "quarter",
    "quarter": "semi",
    "semi": "final",
}
PLAYOFF_STAGES = list(STAGE_ORDER)
STAGE_FOR_FIELD = {
    2: "final",
    4: "semi",
    8: "quarter",
    16: "round_of_16",
    32: "round_of_32",
    64: "round_of_64",
}
MAX_FIELD_SIZE = 64

class BracketError(ValueError):
    """Error de negocio de la llave eliminatoria."""


class BracketConflictError(BracketError):
    """La llave tiene dependencias disputadas y no puede modificarse."""


def parse_sets(score: str) -> tuple[int, int, int, int]:
    """Devuelve (sets_ganados_t1, sets_ganados_t2, games_t1, games_t2).

    El super tie-break (con o sin corchetes) no cuenta como set ni suma games.
    """
    sets, _, _ = classify_score(score)
    sets_t1 = sum(a > b for a, b in sets)
    sets_t2 = sum(b > a for a, b in sets)
    games_t1 = sum(a for a, b in sets)
    games_t2 = sum(b for a, b in sets)
    return sets_t1, sets_t2, games_t1, games_t2


def has_super(score: str) -> bool:
    """Indica si el marcador incluye super tie-break (con o sin corchetes)."""
    _, supers, _ = classify_score(score)
    return bool(supers)


def _is_match_decisive(match: Match) -> bool:
    if match.winner_id is None:
        return False
    if match.is_walkover:
        return not match.score and (match.walkover_reason or "").strip() != ""
    return (match.score or "").strip() != ""


def is_zone_complete(zone: Zone) -> bool:
    """Una zona sólo está completa si todos sus partidos tienen resultado válido."""
    teams = list(zone.teams.all())
    if not teams:
        return False
    expected = len(teams) * (len(teams) - 1) // 2
    matches = list(zone.matches.filter(stage="group"))
    if len(matches) < expected:
        return False
    seen: set[tuple[int, int]] = set()
    for match in matches:
        if not _is_match_decisive(match):
            return False
        if match.team1_id is None or match.team2_id is None:
            return False
        if match.team1_id == match.team2_id:
            return False
        key = tuple(sorted((match.team1_id, match.team2_id)))
        if key in seen:
            return False
        seen.add(key)
    return len(seen) >= expected


def basic_zone_standings(zone: Zone) -> list[dict]:
    """Tabla informativa basada en partidos jugados; puede usarse incompleta."""
    teams = list(zone.teams.all())
    stats = {
        t.id: {
            "team": t,
            "played": 0,
            "won": 0,
            "lost": 0,
            "points": 0,
            "sets_w": 0,
            "sets_l": 0,
            "games_w": 0,
            "games_l": 0,
        }
        for t in teams
    }
    for match in zone.matches.filter(stage="group"):
        if not match.winner_id or not match.team1_id or not match.team2_id:
            continue
        t1, t2 = match.team1, match.team2
        if match.is_walkover:
            s1 = s2 = g1 = g2 = 0
        else:
            s1, s2, g1, g2 = parse_sets(match.score)
        stats[t1.id]["played"] += 1
        stats[t2.id]["played"] += 1
        stats[t1.id]["sets_w"] += s1
        stats[t1.id]["sets_l"] += s2
        stats[t2.id]["sets_w"] += s2
        stats[t2.id]["sets_l"] += s1
        stats[t1.id]["games_w"] += g1
        stats[t1.id]["games_l"] += g2
        stats[t2.id]["games_w"] += g2
        stats[t2.id]["games_l"] += g1
        winner = match.winner
        loser = t2 if winner == t1 else t1
        stats[winner.id]["won"] += 1
        stats[winner.id]["points"] += 3
        stats[loser.id]["lost"] += 1

    rows = list(stats.values())
    for row in rows:
        row["set_diff"] = row["sets_w"] - row["sets_l"]
        row["game_diff"] = row["games_w"] - row["games_l"]

    rows.sort(key=lambda row: (-row["points"], -row["set_diff"], -row["game_diff"]))
    zones_complete = is_zone_complete(zone)
    for index, row in enumerate(rows, start=1):
        row["position"] = index
        row["order"] = index
        row["ranking_value"] = (row["points"], row["set_diff"], row["game_diff"])
        row["zone_complete"] = zones_complete
        row["official"] = zones_complete
    return rows


# Compatibilidad con código existente.
def zone_standings(zone: Zone) -> list[dict]:
    return basic_zone_standings(zone)


def _baseline_key(row: dict):
    return (-row["points"], -row["set_diff"], -row["game_diff"])


def _match_signature(zone: Zone) -> str:
    matches = list(zone.matches.filter(stage="group").order_by("slot", "id"))
    data = [
        {
            "id": m.id,
            "team1": m.team1_id,
            "team2": m.team2_id,
            "winner": m.winner_id,
            "score": (m.score or "").strip(),
            "walkover": bool(m.is_walkover),
            "reason": (m.walkover_reason or "").strip(),
        }
        for m in matches
    ]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _tournament_signature(tournament: Tournament) -> str:
    matches = Match.objects.filter(tournament=tournament, stage="group").order_by("id")
    data = [
        {
            "id": m.id,
            "team1": m.team1_id,
            "team2": m.team2_id,
            "winner": m.winner_id,
            "score": (m.score or "").strip(),
            "walkover": bool(m.is_walkover),
        }
        for m in matches
    ]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _resolve_random_tie(
    tournament: Tournament,
    scope_type: str,
    scope_key: str,
    input_signature: str,
    tied_ids: list[int],
    zone: Zone | None = None,
    actor=None,
) -> list[int]:
    """Resuelve un empate con sorteo auditable y reproducible (append-only)."""
    if len(tied_ids) <= 1:
        return list(tied_ids)

    existing = None
    qs = TieBreakAudit.objects.filter(
        tournament=tournament,
        scope_key=scope_key,
        input_signature=input_signature,
    ).order_by("-created_at")
    for audit in qs:
        if audit.scope_type == scope_type and sorted(audit.participants_ordered) == sorted(tied_ids):
            existing = audit
            break

    if existing is not None:
        result = [int(x) for x in existing.result_ordered]
        result = [x for x in result if x in tied_ids]
        for missing in tied_ids:
            if missing not in result:
                result.append(missing)
        return result

    seed = secrets.randbits(63)
    rng = random.Random(seed)
    participants = list(tied_ids)
    rng.shuffle(participants)
    TieBreakAudit.objects.create(
        tournament=tournament,
        zone=zone,
        scope_type=scope_type,
        scope_key=scope_key,
        input_signature=input_signature,
        tiebreaker_seed=seed,
        participants_ordered=list(tied_ids),
        result_ordered=participants,
        criteria_summary={"criteria": ["points", "set_diff", "game_diff"], "tie_size": len(tied_ids)},
        triggered_by=actor,
    )
    return [int(x) for x in participants]


def official_zone_standings(zone: Zone, actor=None) -> list[dict]:
    if not is_zone_complete(zone):
        raise ValueError(f"La zona {zone} está incompleta; no puede clasificar oficialmente.")

    rows = basic_zone_standings(zone)
    input_signature = _match_signature(zone)
    tournament = zone.tournament
    grouped: list[list[dict]] = []
    for row in rows:
        if not grouped or _baseline_key(grouped[-1][-1]) != _baseline_key(row):
            grouped.append([row])
        else:
            grouped[-1].append(row)

    ordered: list[dict] = []
    scope_key = f"zone:{zone.id}:standings:v1"
    for group in grouped:
        if len(group) == 1:
            ordered.append(group[0])
            continue
        ids = [row["team"].id for row in group]
        resolved_ids = _resolve_random_tie(
            tournament=tournament,
            scope_type="zone_standings",
            scope_key=scope_key,
            input_signature=input_signature,
            tied_ids=ids,
            zone=zone,
            actor=actor,
        )
        by_id = {row["team"].id: row for row in group}
        ordered.extend(by_id[id_] for id_ in resolved_ids if id_ in by_id)

    for index, row in enumerate(ordered, start=1):
        row["position"] = index
        row["order"] = index
        row["official"] = True
        row["zone_complete"] = True
    return ordered


def qualified_teams_detailed(tournament: Tournament) -> list[dict]:
    """Clasificados ordenados: primero los mejores primeros puestos de cada zona,
    después los mejores segundos, etc. Incluye la zona y posición de cada dupla."""
    zones = list(tournament.zones.all().order_by("order", "name"))
    if not zones:
        raise BracketError("El torneo no tiene zonas configuradas.")
    k = tournament.qualifiers_per_zone
    for zone in zones:
        if not is_zone_complete(zone):
            raise BracketError(f"La zona {zone.name} no está completa; clasificación oficial bloqueada.")

    standings_by_zone = [(zone, official_zone_standings(zone)) for zone in zones]
    signature = _tournament_signature(tournament)

    ranked: list[dict] = []
    for position in range(1, k + 1):
        group: list[tuple[Zone, dict]] = []
        for zone, standings in standings_by_zone:
            if len(standings) >= position:
                group.append((zone, standings[position - 1]))
        if not group:
            continue
        group.sort(key=lambda item: (-item[1]["points"], -item[1]["set_diff"], -item[1]["game_diff"]))
        # Separar empates reales (mismos tres criterios) y resolver con sorteo auditable.
        ordered_group: list[tuple[Zone, dict]] = []
        pending: list[tuple[Zone, dict]] = []
        for item in group:
            if pending and _baseline_key(pending[-1][1]) != _baseline_key(item[1]):
                ordered_group.extend(_flush_tie(tournament, pending, position, signature))
                pending = [item]
            else:
                pending.append(item)
        if pending:
            ordered_group.extend(_flush_tie(tournament, pending, position, signature))
        ranked.extend(
            {
                "team": row["team"],
                "zone": zone,
                "zone_position": position,
                "points": row["points"],
                "set_diff": row["set_diff"],
                "game_diff": row["game_diff"],
            }
            for zone, row in ordered_group
        )
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
    if not ranked:
        raise BracketError("No se pudieron determinar equipos clasificados.")
    return ranked


def _flush_tie(tournament, pending, position, signature):
    if len(pending) == 1:
        return list(pending)
    tied_ids = [item[1]["team"].id for item in pending]
    resolved = _resolve_random_tie(
        tournament=tournament,
        scope_type="cross_zone_position",
        scope_key=f"tournament:{tournament.id}:position:{position}",
        input_signature=signature,
        tied_ids=tied_ids,
    )
    by_id = {item[1]["team"].id: item for item in pending}
    return [by_id[id_] for id_ in resolved if id_ in by_id]


def qualified_teams(tournament: Tournament) -> list:
    """Lista de duplas clasificadas a playoffs, ordenadas por ranking oficial."""
    return [item["team"] for item in qualified_teams_detailed(tournament)]


def _next_power_of_two(n: int) -> int:
    p = 1
    while p < n:
        p *= 2
    return p if p >= 2 else 2


def _stage_for_field_size(p: int) -> str:
    if p in STAGE_FOR_FIELD:
        return STAGE_FOR_FIELD[p]
    raise BracketError(
        f"Cantidad de clasificados no soportada ({p}); máximo {MAX_FIELD_SIZE} duplas en la llave."
    )


def _stage_sequence(p: int) -> list[str]:
    stages = [_stage_for_field_size(p)]
    while stages[-1] != "final":
        stages.append(NEXT_STAGE[stages[-1]])
    return stages


def _seed_order(n: int) -> list[int]:
    """Orden clásico de siembra: 1,8,4,5,2,7,3,6 para n=8."""
    if n == 1:
        return [1]
    previous = _seed_order(n // 2)
    result: list[int] = []
    for seed in previous:
        result.extend([seed, n + 1 - seed])
    return result


_BRUTE_FORCE_LIMIT = 12


def _pairing_score(
    pairs: list[tuple[int, int]],
    lower_ids: list[int],
    team_zone: dict[int, int | None],
) -> tuple:
    """Menor es mejor: (cruces de zona, desvío de siembra espejo, firma)."""
    count = len(lower_ids)
    conflicts = 0
    deviation = 0
    for a, b in pairs:
        zone_a = team_zone.get(lower_ids[a])
        if zone_a is not None and zone_a == team_zone.get(lower_ids[b]):
            conflicts += 1
        low, high = (a, b) if a <= b else (b, a)
        deviation += abs(high - (count - 1 - low))
    return (conflicts, deviation, tuple(sorted((min(a, b), max(a, b)) for a, b in pairs)))


def _best_pairings(lower_ids: list[int], team_zone: dict[int, int | None]) -> list[tuple[int, int]]:
    """Empareja duplas evitando cruces de la misma zona y buscando siembra espejo.

    Para llaves chicas se resuelve de forma exacta por búsqueda exhaustiva. Para
    llaves grandes (más de ``_BRUTE_FORCE_LIMIT`` duplas) se parte del
    emparejamiento espejo y se mejora con intercambios deterministas de parejas,
    que es equivalente en los casos reales y no explota combinatoriamente.
    """
    count = len(lower_ids)
    if count == 0:
        return []

    if count <= _BRUTE_FORCE_LIMIT:
        best: dict = {"score": None, "pairs": None}

        def recurse(remaining: list[int], current_pairs: list[tuple[int, int]]):
            if not remaining:
                signature = _pairing_score(current_pairs, lower_ids, team_zone)
                if best["score"] is None or signature < best["score"]:
                    best["score"] = signature
                    best["pairs"] = list(current_pairs)
                return
            first = remaining[0]
            rest = remaining[1:]
            for index, candidate in enumerate(rest):
                others = rest[:index] + rest[index + 1:]
                recurse(others, current_pairs + [(first, candidate)])

        recurse(list(range(count)), [])
        return [(lower_ids[a], lower_ids[b]) for a, b in best["pairs"] or []]

    # Llaves grandes: espejo perfecto (desvío 0) y mejoras 2-opt.
    pairs = [(index, count - 1 - index) for index in range(count // 2)]
    score = _pairing_score(pairs, lower_ids, team_zone)
    for _ in range(200):
        candidate_pairs = None
        candidate_score = score
        for i in range(len(pairs)):
            for j in range(i + 1, len(pairs)):
                a, b = pairs[i]
                c, d = pairs[j]
                base = pairs[:i] + pairs[i + 1:j] + pairs[j + 1:]
                for swapped in (((a, c), (b, d)), ((a, d), (b, c))):
                    proposal = base + list(swapped)
                    proposal_score = _pairing_score(proposal, lower_ids, team_zone)
                    if proposal_score < candidate_score:
                        candidate_score = proposal_score
                        candidate_pairs = proposal
        if candidate_pairs is None:
            break
        pairs = candidate_pairs
        score = candidate_score
    return [(lower_ids[a], lower_ids[b]) for a, b in pairs]


def _assert_no_real_playoff_data(tournament: Tournament) -> None:
    qs = Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES)
    for match in qs:
        if match.has_real_data:
            raise BracketConflictError(
                "La llave ya tiene resultados, walkovers o datos parciales; "
                "no puede regenerarse automáticamente. Revisá los partidos disputados."
            )


def _bracket_structure_ok(
    tournament: Tournament,
    matches: list[Match],
    n: int,
    p: int,
    detailed: list[dict] | None = None,
) -> bool:
    if len(matches) != p - 1:
        return False
    stages = _stage_sequence(p)
    by_depth: dict[int, list[Match]] = {}
    for match in matches:
        if match.stage not in stages:
            return False
        by_depth.setdefault(stages.index(match.stage) + 1, []).append(match)
    if sorted(by_depth) != list(range(1, len(stages) + 1)):
        return False
    for depth, rows in by_depth.items():
        if len(rows) != p // (2 ** depth):
            return False
        for row in rows:
            if depth < len(stages) and row.next_match_id is None:
                return False
    first_round = by_depth.get(1, [])
    if sum(1 for m in first_round if m.is_bye) != p - n:
        return False
    if detailed is not None:
        plan = _build_first_round(detailed, p, p - n)
        first_round.sort(key=lambda row: row.slot)
        if len(first_round) != len(plan):
            return False
        for row, entry in zip(first_round, plan):
            expected = [team.pk if team else None for team in entry["teams"]]
            if [row.team1_id, row.team2_id] != expected:
                return False
            if bool(row.is_bye) != bool(entry["is_bye"]):
                return False
    return True


def _build_first_round(detailed: list[dict], p: int, byes: int) -> list[dict]:
    """Devuelve las llaves de primera ronda: cada una con teams[2] (None en byes) e is_bye.

    Las posiciones de siembra cuyos números superan la cantidad de clasificados
    quedan vacías: la dupla que las enfrenta recibe el descanso.
    """
    n = len(detailed)
    order = _seed_order(p)
    by_seed = {index + 1: item for index, item in enumerate(detailed)}
    real = detailed[byes:]
    team_zone = {item["team"].id: item["zone"].id for item in real}
    real_pairs = _best_pairings([item["team"].id for item in real], team_zone)
    rank_of = {item["team"].id: item["rank"] for item in detailed}

    slots = [(order[2 * j], order[2 * j + 1]) for j in range(p // 2)]
    real_slots = [slot for slot in slots if slot[0] <= n and slot[1] <= n]
    bye_slots = [slot for slot in slots if (slot[0] > n) != (slot[1] > n)]
    real_slots.sort(key=lambda slot: min(slot))

    plan: list[dict] = []
    for slot in bye_slots:
        if slot[0] <= n:
            bye_item, empty_seed = by_seed[slot[0]], slot[1]
            plan.append({"teams": [bye_item["team"], None], "is_bye": True, "seeds": [slot[0], empty_seed]})
        else:
            bye_item, empty_seed = by_seed[slot[1]], slot[0]
            plan.append({"teams": [None, bye_item["team"]], "is_bye": True, "seeds": [empty_seed, slot[1]]})

    for index, (team_a_id, team_b_id) in enumerate(real_pairs):
        slot = real_slots[index]
        low_seed_side = 0 if slot[0] <= slot[1] else 1
        first, second = (
            (team_a_id, team_b_id) if rank_of[team_a_id] < rank_of[team_b_id] else (team_b_id, team_a_id)
        )
        teams: list = [None, None]
        teams[low_seed_side] = next(item["team"] for item in real if item["team"].id == first)
        teams[1 - low_seed_side] = next(item["team"] for item in real if item["team"].id == second)
        plan.append({"teams": teams, "is_bye": False, "seeds": list(slot)})

    plan_with_slots = []
    for entry in plan:
        s1, s2 = entry["seeds"]
        slot_index = slots.index((s1, s2)) if (s1, s2) in slots else slots.index((s2, s1))
        plan_with_slots.append((slot_index, entry))
    plan_with_slots.sort(key=lambda pair: pair[0])
    return [entry for _, entry in plan_with_slots]


def playoff_bracket_info(tournament: Tournament) -> dict:
    """Datos comunes para armar la llave: clasificadas, tamaño y descansos.

    Lanza ``BracketError`` si las zonas no están completas o la cantidad de
    clasificadas no es soportada.
    """
    detailed = qualified_teams_detailed(tournament)
    n = len(detailed)
    if n < 2:
        raise BracketError("Se necesitan al menos 2 duplas clasificadas.")
    p = _next_power_of_two(n)
    if p > MAX_FIELD_SIZE:
        raise BracketError(
            f"Cantidad de clasificados no soportada ({n}); máximo {MAX_FIELD_SIZE} duplas (treintaidosavos)."
        )
    return {"detailed": detailed, "n": n, "p": p, "matches": p // 2, "byes": p - n}


def _create_playoff_tree(tournament: Tournament, plan: list[dict], p: int, *, manual: bool) -> list[Match]:
    """Crea el árbol completo de la eliminatoria con el plan de primera ronda dado.

    ``plan`` es una lista por posición de primera ronda:
    ``{"teams": [Team|None, Team|None], "is_bye": bool}``.
    """
    stages = _stage_sequence(p)
    with transaction.atomic():
        locked = Tournament.objects.select_for_update().get(pk=tournament.pk)
        _assert_no_real_playoff_data(locked)
        Match.objects.filter(tournament=locked, stage__in=PLAYOFF_STAGES).delete()

        created: dict[tuple[int, int], Match] = {}
        for depth, stage in enumerate(stages, start=1):
            matches_in_round = p // (2 ** depth)
            for slot in range(1, matches_in_round + 1):
                row = Match(
                    tournament=locked,
                    stage=stage,
                    round_number=depth,
                    slot=slot,
                    bracket_node=f"{stage}-r{depth}-m{slot}",
                )
                row._skip_auto_progress = True
                row.save()
                created[(depth, slot)] = row

        # Enlaces entre rondas.
        for depth in range(1, len(stages)):
            for slot in range(1, p // (2 ** depth) + 1):
                row = created[(depth, slot)]
                parent = created[(depth + 1, (slot + 1) // 2)]
                row.next_match = parent
                row.next_slot = 1 if slot % 2 == 1 else 2
                row._skip_auto_progress = True
                row.save()

        # Primera ronda con participantes reales y descansos.
        first_round: list[Match] = []
        for index, entry in enumerate(plan, start=1):
            row = created[(1, index)]
            row.team1 = entry["teams"][0]
            row.team2 = entry["teams"][1]
            row.is_bye = entry["is_bye"]
            if entry["is_bye"]:
                row.winner = entry["teams"][0] or entry["teams"][1]
            row._skip_auto_progress = True
            row.save()
            first_round.append(row)

        # Avanzar descansos a la ronda siguiente.
        sync_bracket_progression(locked)

        updates: dict = {"manual_bracket": manual}
        if locked.status in ("draft", "open"):
            updates["status"] = "running"
        Tournament.objects.filter(pk=locked.pk).update(**updates)
        # Mantener sincronizada la instancia recibida (evita lecturas viejas en el mismo flujo).
        for field, value in updates.items():
            setattr(tournament, field, value)
        return first_round


def generate_playoff_bracket(tournament: Tournament) -> list[Match]:
    """Genera el árbol completo de la eliminatoria (siembra automática) en una transacción.

    Devuelve los partidos de la primera ronda. Idempotente mientras no existan
    resultados reales: regenera la estructura si hay datos sólo estructurales.
    """
    if not tournament.playoffs_enabled:
        raise BracketError("Este torneo es sólo round robin: no genera llaves eliminatorias.")
    info = playoff_bracket_info(tournament)
    p = info["p"]
    plan = _build_first_round(info["detailed"], p, info["byes"])
    return _create_playoff_tree(tournament, plan, p, manual=False)


def generate_playoff_bracket_manual(tournament: Tournament, selections: list[tuple]) -> list[Match]:
    """Crea la llave con los cruces elegidos a mano.

    ``selections`` es una lista por posición de primera ronda con pares
    ``(team1_id | None, team2_id | None)``. Un lugar vacío representa un
    descanso (bye). Se exige que cada cruce tenga al menos una dupla y que
    todas las clasificadas queden ubicadas exactamente una vez.
    """
    if not tournament.playoffs_enabled:
        raise BracketError("Este torneo es sólo round robin: no genera llaves eliminatorias.")
    info = playoff_bracket_info(tournament)
    detailed = info["detailed"]
    p = info["p"]
    expected = p // 2
    if len(selections) != expected:
        raise BracketError(f"Definí los {expected} cruces de la primera ronda.")
    pool = {entry["team"].pk: entry["team"] for entry in detailed}
    seen: set[int] = set()
    plan: list[dict] = []
    for index, selection in enumerate(selections, start=1):
        raw1, raw2 = selection
        chosen: list[Team | None] = []
        for raw in (raw1, raw2):
            if raw in (None, "", "0", 0):
                chosen.append(None)
                continue
            if not str(raw).isdigit() or int(raw) not in pool:
                raise BracketError(f"En el cruce {index} hay una dupla que no clasificó a la llave.")
            chosen.append(pool[int(raw)])
        team1, team2 = chosen
        if team1 is not None and team2 is not None and team1.pk == team2.pk:
            raise BracketError(f"El cruce {index} tiene la misma dupla en los dos lugares.")
        if team1 is None and team2 is None:
            raise BracketError(
                f"El cruce {index} quedó vacío: elegí al menos una dupla (un descanso deja un lugar libre)."
            )
        for team in (team1, team2):
            if team is None:
                continue
            if team.pk in seen:
                raise BracketError(f"La dupla «{team.display_name}» está ubicada más de una vez.")
            seen.add(team.pk)
        plan.append({"teams": [team1, team2], "is_bye": team1 is None or team2 is None})
    missing = sorted(team.display_name for team in pool.values() if team.pk not in seen)
    if missing:
        raise BracketError("Faltan clasificadas por ubicar: " + ", ".join(missing) + ".")
    return _create_playoff_tree(tournament, plan, p, manual=True)


def _expected_bracket(tournament: Tournament) -> tuple[int, int]:
    detailed = qualified_teams_detailed(tournament)
    n = len(detailed)
    return n, _next_power_of_two(n)


def _first_round_pool_ids(matches: list[Match]) -> set[int]:
    """Ids de las duplas ubicadas en la primera ronda de la llave."""
    if not matches:
        return set()
    first_depth = min(row.round_number for row in matches)
    return {
        team_id
        for row in matches
        if row.round_number == first_depth
        for team_id in (row.team1_id, row.team2_id)
        if team_id
    }


def ensure_playoff_bracket(tournament: Tournament) -> list[Match]:
    """Garantiza que exista un cuadro coherente; no toca datos disputados."""
    if not tournament.playoffs_enabled:
        return []
    zones = list(tournament.zones.all())
    if not zones or any(not is_zone_complete(zone) for zone in zones):
        raise BracketError("Las zonas no están completas; no se puede generar la llave.")
    expected_qualifiers = sum(
        min(tournament.qualifiers_per_zone, zone.teams.count()) for zone in zones
    )
    if expected_qualifiers < 2:
        return []
    detailed = qualified_teams_detailed(tournament)
    n = len(detailed)
    p = _next_power_of_two(n)
    existing = list(
        Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES)
        .select_related("team1", "team2", "winner", "next_match")
        .order_by("round_number", "slot")
    )
    if existing:
        if tournament.manual_bracket:
            # Cruces armados a mano: se conservan mientras la estructura sea
            # coherente y participen exactamente las clasificadas actuales
            # (el orden elegido por el usuario no se re-siembra).
            qualified_ids = {entry["team"].pk for entry in detailed}
            if _bracket_structure_ok(tournament, existing, n, p) and _first_round_pool_ids(existing) == qualified_ids:
                return existing
        elif _bracket_structure_ok(tournament, existing, n, p, detailed=detailed):
            return existing
        # La estructura no coincide con la clasificación actual.
        _assert_no_real_playoff_data(tournament)
    generate_playoff_bracket(tournament)
    return list(
        Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES)
        .select_related("team1", "team2", "winner", "next_match")
        .order_by("round_number", "slot")
    )


def sync_bracket_progression(tournament: Tournament, actor=None) -> list[Match]:
    """Coloca cada ganador en el partido siguiente correspondiente.

    Es idempotente. Si un partido siguiente ya tiene datos reales y no coincide
    con el ganador entrante, lanza ``BracketConflictError``.
    """
    with transaction.atomic():
        locked = Tournament.objects.select_for_update().get(pk=tournament.pk)
        matches = list(
            Match.objects.filter(tournament=locked, stage__in=PLAYOFF_STAGES)
            .select_related("team1", "team2", "winner")
            .order_by("round_number", "slot")
        )
        by_id = {row.id: row for row in matches}
        for row in matches:
            if row.winner_id is None or row.next_match_id is None:
                continue
            target = by_id.get(row.next_match_id)
            if target is None:
                raise BracketConflictError("El partido siguiente no existe en la llave.")
            field = "team1_id" if row.next_slot == 1 else "team2_id"
            current = getattr(target, field)
            if current == row.winner_id:
                continue
            if target.has_real_data:
                raise BracketConflictError(
                    f"El partido siguiente ({target.display_stage}, posición {target.slot}) "
                    "ya tiene resultado cargado. Corregilo antes de continuar."
                )
            # Sin datos reales en el partido siguiente, la posición es estructural:
            # se actualiza de forma controlada al ganador correcto.
            Match.objects.filter(pk=target.pk).update(**{field: row.winner_id})
            setattr(target, field, row.winner_id)

        final_rows = [row for row in matches if row.stage == "final"]
        if final_rows and all(row.winner_id is not None for row in final_rows):
            Tournament.objects.filter(pk=locked.pk).update(status="finished")
        return matches


def advance_stage(tournament: Tournament, from_stage: str) -> list[Match]:
    """Compatibilidad: sincroniza el avance y devuelve la etapa siguiente."""
    if from_stage not in NEXT_STAGE:
        return []
    sync_bracket_progression(tournament)
    return list(
        Match.objects.filter(tournament=tournament, stage=NEXT_STAGE[from_stage]).order_by("slot", "id")
    )


def first_round_matches(tournament: Tournament) -> list[Match]:
    matches = list(
        Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES).order_by("round_number", "slot")
    )
    if not matches:
        return []
    first_depth = min(row.round_number for row in matches)
    return [row for row in matches if row.round_number == first_depth]


def bracket_rounds(tournament: Tournament) -> list[dict]:
    """Estructura para la vista de llaves: [{stage, label, matches: [...]}]"""
    matches = list(
        Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES)
        .select_related("team1", "team2", "winner", "team1__player1", "team1__player2", "team2__player1", "team2__player2")
        .order_by("round_number", "slot")
    )
    rounds: dict[int, list[Match]] = {}
    labels: dict[int, str] = {}
    for row in matches:
        rounds.setdefault(row.round_number, []).append(row)
        labels[row.round_number] = row.display_stage
    return [
        {"depth": depth, "label": labels[depth], "matches": rounds[depth]}
        for depth in sorted(rounds)
    ]


def pending_zone_summary(tournament: Tournament) -> list[dict]:
    """Partidos de zona sin resultado válido, agrupados por zona."""
    summary = []
    for zone in tournament.zones.all().order_by("order", "name"):
        pending = []
        for match in zone.matches.filter(stage="group").order_by("slot", "id"):
            if not _is_match_decisive(match):
                pending.append(match)
        summary.append({"zone": zone, "pending": pending})
    return summary


__all__ = [
    "STAGE_ORDER",
    "NEXT_STAGE",
    "PLAYOFF_STAGES",
    "BracketError",
    "BracketConflictError",
    "parse_sets",
    "has_super",
    "is_zone_complete",
    "zone_standings",
    "basic_zone_standings",
    "official_zone_standings",
    "qualified_teams",
    "qualified_teams_detailed",
    "playoff_bracket_info",
    "generate_playoff_bracket",
    "generate_playoff_bracket_manual",
    "ensure_playoff_bracket",
    "advance_stage",
    "sync_bracket_progression",
    "first_round_matches",
    "bracket_rounds",
    "pending_zone_summary",
]
