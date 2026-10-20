"""Vistas de Pádel Server.

- Panel de gestión propio (sin depender del admin de Django) con selección de
  torneo y secciones: resumen, parejas, zonas, partidos, resultados,
  clasificación y llaves.
- Vistas públicas de consulta.
- Toda operación valida permisos de complejo/torneo y reglas en el servidor.
"""

from __future__ import annotations

import re
from datetime import date as date_cls, time as time_cls, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.core.validators import URLValidator, validate_email
from django.db import IntegrityError, transaction
from django.db.models import Count, ProtectedError, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import access
from core.models import (
    Complex,
    Court,
    CourtBooking,
    CourtDateOverride,
    CourtSchedule,
    Invitation,
    Match,
    Player,
    Registration,
    RegistrationRequest,
    SchedulePause,
    Team,
    TemporaryPassword,
    Tournament,
    TournamentPlayer,
    Zone,
    ZoneTeam,
)
from core.notifications import notify_court_booking, notify_new_signup_request, notify_tournament_team_signup
from core.services import draws
from core.services import matches as matches_service
from core.services import players as players_service
from core.services import schedule as schedule_service
from core.services.tournament_workflow import register_match_result

PLAYOFF_STAGES = draws.PLAYOFF_STAGES


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        if hasattr(exc, "message_dict"):
            parts = []
            for field, messages in exc.message_dict.items():
                prefix = "" if field == "__all__" else f"{field}: "
                parts.extend(f"{prefix}{m}" for m in messages)
            return " ".join(parts)
        return " ".join(exc.messages)
    if isinstance(exc, ProtectedError):
        return "La operación afecta datos relacionados y no puede completarse."
    return str(exc)


def _run_action(request, action, success_message: str):
    try:
        action()
        messages.success(request, success_message)
        return True
    except (ValidationError, ProtectedError, PermissionDenied, draws.BracketError) as exc:
        messages.error(request, _error_text(exc))
        return False
    except IntegrityError:
        messages.error(request, "La operación viola una restricción de integridad. Revisá los datos.")
        return False


def _tournament_or_404(request, slug: str) -> Tournament:
    tournament = get_object_or_404(Tournament.objects.select_related("complex"), slug=slug)
    access.require_tournament_access(request.user, tournament)
    return tournament


def _panel_context(request, tournament: Tournament, section: str, **extra) -> dict:
    nav = [
        ("resumen", "Resumen", reverse("tournament_manage", args=[tournament.slug])),
        ("parejas", "Parejas", reverse("panel_teams", args=[tournament.slug])),
        ("jugadores", "Jugadores", reverse("panel_players", args=[tournament.slug])),
        ("zonas", "Zonas", reverse("panel_zones", args=[tournament.slug])),
        ("partidos", "Partidos", reverse("panel_matches", args=[tournament.slug])),
        ("resultados", "Resultados", reverse("panel_results", args=[tournament.slug])),
        ("clasificacion", "Clasificación", reverse("panel_standings", args=[tournament.slug])),
        ("llaves", "Llaves", reverse("panel_bracket", args=[tournament.slug])),
    ]
    context = {
        "tournament": tournament,
        "section": section,
        "nav": nav,
        "can_manage": access.user_can_access_tournament(request.user, tournament),
    }
    context.update(extra)
    return context


# ---------------------------------------------------------------------------
# Selección de torneo y panel
# ---------------------------------------------------------------------------


def home(request):
    """Portada pública: sacar turno, torneos y acceso al panel."""
    complexes = Complex.objects.filter(is_active=True).order_by("name")
    tournaments = (
        Tournament.objects.filter(status__in=["open", "running"])
        .select_related("complex")
        .order_by("-start_date", "name")[:3]
    )
    return render(
        request,
        "home.html",
        {
            "public_section": "inicio",
            "complexes": complexes,
            "tournaments": tournaments,
        },
    )


@login_required
def tournament_select(request):
    tournaments = access.scoped_tournaments(request.user).select_related("complex").order_by("-start_date", "name")
    selected = _selected_tournament(request)
    if selected is not None and not access.user_can_access_tournament(request.user, selected):
        request.session.pop("padel_tournament_id", None)
        selected = None
    return render(
        request,
        "tournament_select.html",
        {
            "tournaments": tournaments,
            "selected": selected,
        },
    )


@login_required
def tournament_choose(request, slug):
    tournament = get_object_or_404(Tournament.objects.select_related("complex"), slug=slug)
    access.require_tournament_access(request.user, tournament)
    request.session["padel_tournament_id"] = tournament.pk
    request.session["padel_tournament_slug"] = tournament.slug
    messages.success(request, f"Torneo seleccionado: {tournament.name}")
    return redirect("tournament_manage", slug=tournament.slug)


@login_required
def tournament_exit(request):
    request.session.pop("padel_tournament_id", None)
    request.session.pop("padel_tournament_slug", None)
    return redirect("tournament_select")


def _selected_tournament(request):
    tournament_id = request.session.get("padel_tournament_id")
    if not tournament_id:
        return None
    return Tournament.objects.filter(pk=tournament_id).first()


@login_required
def tournament_manage(request, slug):
    tournament = _tournament_or_404(request, slug)
    zones = list(tournament.zones.all().order_by("order", "name"))
    group_matches = list(
        Match.objects.filter(tournament=tournament, stage="group").select_related("zone", "team1", "team2", "winner")
    )
    playoff_matches = list(
        Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES).select_related("team1", "team2", "winner")
    )
    zones_complete = bool(zones) and all(draws.is_zone_complete(zone) for zone in zones)
    qualifiers_total = len(zones) * tournament.qualifiers_per_zone
    context = _panel_context(
        request,
        tournament,
        "resumen",
        teams_count=len(players_service.tournament_team_ids(tournament)),
        zones=zones,
        zones_complete=zones_complete,
        group_total=len(group_matches),
        group_played=sum(1 for m in group_matches if m.is_decided or m.is_walkover),
        group_pending=sum(1 for m in group_matches if not (m.is_decided and (m.score or m.is_walkover))),
        playoff_count=len(playoff_matches),
        playoff_pending=sum(1 for m in playoff_matches if not m.is_bye and m.winner_id is None),
        qualifiers_total=qualifiers_total,
        bracket_exists=bool(playoff_matches),
        can_generate_bracket=(
            tournament.playoffs_enabled and zones_complete and qualifiers_total >= 2 and not playoff_matches
        ),
        pending_zones=draws.pending_zone_summary(tournament),
    )
    return render(request, "panel/summary.html", context)


# ---------------------------------------------------------------------------
# Crear / editar torneo
# ---------------------------------------------------------------------------


@login_required
def tournament_form(request, slug=None):
    tournament = None
    if slug:
        tournament = _tournament_or_404(request, slug)
    complexes = Complex.objects.all() if request.user.is_superuser else request.user.complexes.all()
    complexes = list(complexes)
    # Si el usuario tiene un solo complejo a cargo, se elige solo al crear el torneo.
    default_complex = complexes[0] if len(complexes) == 1 else None
    if not request.user.is_superuser and not complexes:
        messages.error(
            request,
            "No tenés complejos asignados. Creá el tuyo desde Configuración general y volvé a intentar.",
        )
        return redirect("tournament_select")
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        start_date = request.POST.get("start_date") or None
        complex_id = request.POST.get("complex") or None
        # Valores por defecto al crear: mejor de 3 sets y estado «Inscripciones».
        default_best_of = 3 if tournament is None else 2
        default_status = "open" if tournament is None else "draft"
        if not name:
            messages.error(request, "El nombre del torneo es obligatorio.")
        elif not start_date:
            messages.error(request, "La fecha de inicio es obligatoria.")
        else:
            try:
                with transaction.atomic():
                    if complex_id:
                        complex_obj = get_object_or_404(Complex, pk=complex_id)
                        access.require_complex_access(request.user, complex_obj)
                    elif tournament is not None:
                        complex_obj = tournament.complex
                    else:
                        complex_obj = default_complex
                    defaults = {
                        "complex": complex_obj,
                        "name": name,
                        "category": request.POST.get("category") or "principiante",
                        "start_date": start_date,
                        "end_date": request.POST.get("end_date") or None,
                        "format_type": request.POST.get("format_type") or "zonas_playoffs",
                        "best_of_sets": int(request.POST.get("best_of_sets") or default_best_of),
                        "teams_per_zone": int(request.POST.get("teams_per_zone") or 3),
                        "qualifiers_per_zone": int(request.POST.get("qualifiers_per_zone") or 2),
                        "status": request.POST.get("status") or default_status,
                    }
                    if tournament is None:
                        from django.utils.text import slugify

                        slug_value = slugify((request.POST.get("slug") or name).strip()) or slugify(name)
                        tournament = Tournament(slug=slug_value, **defaults)
                        tournament.full_clean()
                        tournament.save()
                        messages.success(request, "Torneo creado. Ya podés cargar parejas.")
                    else:
                        for key, value in defaults.items():
                            setattr(tournament, key, value)
                        tournament.full_clean()
                        tournament.save()
                        messages.success(request, "Configuración del torneo actualizada.")
                if "complex" in request.POST or not slug:
                    return redirect("tournament_choose", slug=tournament.slug)
                return redirect("tournament_manage", slug=tournament.slug)
            except (ValidationError, ProtectedError, IntegrityError) as exc:
                messages.error(request, _error_text(exc))
    return render(
        request,
        "panel/tournament_form.html",
        {
            "tournament": tournament,
            "complexes": complexes,
            "default_complex": default_complex,
            "default_best_of_sets": 3 if tournament is None else tournament.best_of_sets,
            "default_status": "open" if tournament is None else tournament.status,
            "categories": Tournament.CATEGORY,
            "formats": Tournament.FORMAT,
            "statuses": Tournament.STATUS,
        },
    )


# ---------------------------------------------------------------------------
# Eliminar torneo
# ---------------------------------------------------------------------------


@login_required
@require_POST
def panel_tournament_delete(request, slug):
    """Elimina el torneo y sus datos de competencia, conservando jugadores y duplas."""
    tournament = _tournament_or_404(request, slug)
    tournament_pk = tournament.pk
    tournament_name = tournament.name

    def action():
        with transaction.atomic():
            tournament.delete()

    ok = _run_action(request, action, f"Torneo «{tournament_name}» eliminado.")
    if request.session.get("padel_tournament_id") == tournament_pk:
        request.session.pop("padel_tournament_id", None)
        request.session.pop("padel_tournament_slug", None)
    if ok:
        messages.info(
            request,
            "Las parejas y los jugadores quedaron guardados en la base para volver a usarlos.",
        )
    return redirect("tournament_select")


# ---------------------------------------------------------------------------
# Parejas
# ---------------------------------------------------------------------------


@login_required
def panel_teams(request, slug):
    tournament = _tournament_or_404(request, slug)
    if tournament.self_registration_enabled and not tournament.registration_code:
        players_service.ensure_registration_code(tournament)
    registrations = list(
        Registration.objects.filter(tournament=tournament)
        .exclude(status="cancelled")
        .select_related("team", "team__player1", "team__player2")
        .order_by("created_at", "id")
    )
    zone_by_team = {
        assignment.team_id: assignment.zone for assignment in ZoneTeam.objects.filter(zone__tournament=tournament).select_related("zone")
    }
    team_rows = []
    pending_rows = []
    for registration in registrations:
        row = {
            "registration": registration,
            "team": registration.team,
            "zone": zone_by_team.get(registration.team_id),
            "matches": Match.objects.filter(tournament=tournament)
            .filter(Q(team1=registration.team) | Q(team2=registration.team))
            .count(),
        }
        if registration.status == "pending":
            row["phones"] = [
                phone
                for phone in (registration.team.player1.phone, registration.team.player2.phone)
                if phone
            ]
            pending_rows.append(row)
        else:
            team_rows.append(row)
    base_signup_url = request.build_absolute_uri(
        reverse("public_tournament_signup", kwargs={"slug": tournament.slug})
    )
    signup_url = base_signup_url
    if tournament.registration_code:
        # El link lleva el código adentro: al abrirlo el formulario ya viene completo.
        signup_url = f"{base_signup_url}?codigo={tournament.registration_code}"
    whatsapp_share_url = "https://wa.me/?text=" + quote(
        f"Anotá tu pareja al torneo «{tournament.name}» 🎾\n{signup_url}"
    )
    whatsapp_code_url = "https://wa.me/?text=" + quote(tournament.registration_code or "")
    context = _panel_context(
        request,
        tournament,
        "parejas",
        team_rows=team_rows,
        pending_rows=pending_rows,
        self_registration_enabled=tournament.self_registration_enabled,
        registration_code=tournament.registration_code,
        signup_url=signup_url,
        whatsapp_share_url=whatsapp_share_url,
        whatsapp_code_url=whatsapp_code_url,
        available_players=players_service.available_players(tournament),
        used_players_count=len(players_service.player_ids_in_tournament(tournament)),
        known_players_total=Player.objects.filter(active=True).count(),
        genders=Player.GENDER,
        levels=Player.LEVEL,
    )
    return render(request, "panel/teams.html", context)


@login_required
@require_POST
def panel_self_registration(request, slug, action):
    """Activa/desactiva la autoinscripción o genera un código nuevo."""
    tournament = _tournament_or_404(request, slug)
    if action == "activar":

        def activate() -> None:
            players_service.ensure_registration_code(tournament)
            tournament.self_registration_enabled = True
            tournament.save(update_fields=["self_registration_enabled"])

        _run_action(request, activate, "Autoinscripción activada: compartí el código con los jugadores.")
    elif action == "desactivar":
        tournament.self_registration_enabled = False
        tournament.save(update_fields=["self_registration_enabled"])
        messages.success(request, "Autoinscripción desactivada. El código queda guardado por si la reactivás.")
    elif action == "regenerar":
        _run_action(
            request,
            lambda: players_service.regenerate_registration_code(tournament),
            "Código nuevo generado: el anterior ya no funciona.",
        )
    else:
        raise Http404("Acción desconocida.")
    return redirect("panel_teams", slug=tournament.slug)


@login_required
@require_POST
def panel_registration_action(request, slug, registration_id, action):
    """Confirma o rechaza una solicitud de autoinscripción."""
    tournament = _tournament_or_404(request, slug)
    registration = get_object_or_404(Registration, pk=registration_id, tournament=tournament)
    if action == "confirmar":

        def confirm() -> None:
            registration.status = "confirmed"
            registration.save()

        _run_action(request, confirm, f"{registration.team} quedó confirmada en el torneo.")
    elif action == "rechazar":
        registration.status = "cancelled"
        registration.save()
        messages.success(request, f"Se rechazó la solicitud de {registration.team}. No entra al torneo.")
    else:
        raise Http404("Acción desconocida.")
    return redirect("panel_teams", slug=tournament.slug)


@login_required
@require_POST
def panel_team_create(request, slug):
    tournament = _tournament_or_404(request, slug)
    name = request.POST.get("name", "")
    created_players: list[str] = []

    def pick_player(slot: str) -> Player:
        raw_id = (request.POST.get(f"{slot}_id") or request.POST.get(slot) or "").strip()
        if raw_id.isdigit():
            return get_object_or_404(Player, pk=int(raw_id))
        raw_name = (request.POST.get(f"{slot}_name") or "").strip()
        if not raw_name:
            ordinal = "1" if slot == "player1" else "2"
            raise ValidationError(f"Elegí un jugador guardado o escribí el nombre del jugador {ordinal}.")
        player, created = players_service.resolve_player(
            raw_name,
            gender=request.POST.get(f"{slot}_gender", ""),
            level=request.POST.get(f"{slot}_level", ""),
        )
        if created:
            created_players.append(str(player))
        return player

    def action():
        with transaction.atomic():
            player1 = pick_player("player1")
            player2 = pick_player("player2")
            players_service.create_team(tournament, player1, player2, name=name)

    ok = _run_action(request, action, "Pareja agregada al torneo.")
    if ok and created_players:
        messages.info(
            request,
            "Jugadores nuevos guardados en la base: "
            + ", ".join(created_players)
            + ". La próxima vez aparecen solos al escribir el nombre.",
        )
    return redirect("panel_teams", slug=tournament.slug)


@login_required
@require_POST
def panel_team_delete(request, slug, team_id):
    tournament = _tournament_or_404(request, slug)
    team = get_object_or_404(Team, pk=team_id)

    def action():
        players_service.release_team(tournament, team)

    _run_action(request, action, "Pareja dada de baja del torneo. Los jugadores quedaron disponibles.")
    return redirect("panel_teams", slug=tournament.slug)


# ---------------------------------------------------------------------------
# Jugadores (registro global reutilizable)
# ---------------------------------------------------------------------------


@login_required
def panel_players(request, slug):
    tournament = _tournament_or_404(request, slug)
    query = (request.GET.get("q") or "").strip()
    status = request.GET.get("estado") or "todos"
    level = request.GET.get("nivel") or "todos"
    players = list(Player.objects.all().order_by("last_name", "first_name"))
    if status == "activos":
        players = [player for player in players if player.active]
    elif status == "inactivos":
        players = [player for player in players if not player.active]
    if level in dict(Player.LEVEL):
        players = [player for player in players if player.level == level]
    if query:
        needle = players_service.normalize_name(query)
        players = [
            player
            for player in players
            if needle in players_service.normalize_name(str(player))
            or needle in players_service.normalize_name(f"{player.first_name} {player.last_name}")
        ]
    counts = {
        row["player_id"]: row["total"]
        for row in TournamentPlayer.objects.filter(player_id__in=[player.pk for player in players])
        .values("player_id")
        .annotate(total=Count("id"))
    }
    in_tournament = players_service.player_ids_in_tournament(tournament)
    rows = [
        {
            "player": player,
            "tournaments": counts.get(player.pk, 0),
            "in_tournament": player.pk in in_tournament,
        }
        for player in players
    ]
    context = _panel_context(
        request,
        tournament,
        "jugadores",
        rows=rows,
        query=query,
        status=status,
        nivel=level,
        total_players=Player.objects.count(),
        active_players=Player.objects.filter(active=True).count(),
        genders=Player.GENDER,
        levels=Player.LEVEL,
    )
    return render(request, "panel/players.html", context)


@login_required
@require_POST
def panel_player_create(request, slug):
    tournament = _tournament_or_404(request, slug)
    holder: dict = {}

    def action():
        first = (request.POST.get("first_name") or "").strip()
        last = (request.POST.get("last_name") or "").strip()
        if not first or not last:
            raise ValidationError("Completá nombre y apellido del jugador.")
        existing = players_service.find_player_by_name(f"{first} {last}")
        if existing is not None:
            raise ValidationError(
                f"Ya existe «{existing}» en la base. Elegilo de la lista o unificá las fichas si es un duplicado."
            )
        gender = request.POST.get("gender") or "M"
        level = request.POST.get("level") or "principiante"
        player = Player(
            first_name=first,
            last_name=last,
            gender=gender if gender in dict(Player.GENDER) else "M",
            level=level if level in dict(Player.LEVEL) else "principiante",
            phone=(request.POST.get("phone") or "").strip(),
            email=(request.POST.get("email") or "").strip(),
        )
        player.full_clean()
        player.save()
        holder["player"] = player

    ok = _run_action(request, action, "Jugador agregado a la base.")
    if ok:
        messages.info(
            request,
            f"«{holder['player']}» quedó guardado y disponible para los próximos torneos.",
        )
    return redirect("panel_players", slug=tournament.slug)


@login_required
def panel_player_edit(request, slug, player_id):
    tournament = _tournament_or_404(request, slug)
    player = get_object_or_404(Player, pk=player_id)
    if request.method == "POST":

        def action():
            player.first_name = (request.POST.get("first_name") or "").strip()
            player.last_name = (request.POST.get("last_name") or "").strip()
            if not player.first_name or not player.last_name:
                raise ValidationError("Completá nombre y apellido del jugador.")
            gender = request.POST.get("gender") or ""
            if gender in dict(Player.GENDER):
                player.gender = gender
            level = request.POST.get("level") or ""
            if level in dict(Player.LEVEL):
                player.level = level
            player.phone = (request.POST.get("phone") or "").strip()
            player.email = (request.POST.get("email") or "").strip()
            player.active = request.POST.get("active") == "1"
            player.full_clean()
            player.save()

        if _run_action(request, action, "Datos del jugador actualizados."):
            return redirect("panel_player_edit", slug=tournament.slug, player_id=player.pk)
    history = list(
        TournamentPlayer.objects.filter(player=player)
        .select_related("tournament", "team")
        .order_by("-tournament__start_date", "tournament__name")
    )
    context = _panel_context(
        request,
        tournament,
        "jugadores",
        player=player,
        history=history,
        others=Player.objects.exclude(pk=player.pk),
        in_tournament=player.pk in players_service.player_ids_in_tournament(tournament),
        genders=Player.GENDER,
        levels=Player.LEVEL,
    )
    return render(request, "panel/player_form.html", context)


@login_required
@require_POST
def panel_player_merge(request, slug, player_id):
    tournament = _tournament_or_404(request, slug)
    source = get_object_or_404(Player, pk=player_id)
    raw_target = (request.POST.get("target") or "").strip()
    if not raw_target.isdigit():
        messages.error(request, "Elegí con qué jugador unificar la ficha duplicada.")
        return redirect("panel_player_edit", slug=tournament.slug, player_id=source.pk)
    target = get_object_or_404(Player, pk=int(raw_target))
    source_name = str(source)
    holder: dict = {}

    def action():
        holder.update(players_service.merge_players(source, target))

    if not _run_action(request, action, f"«{source_name}» se unificó con «{target}»."):
        return redirect("panel_player_edit", slug=tournament.slug, player_id=source.pk)
    moved = ", ".join(
        f"{holder[key]} {label}"
        for key, label in (
            ("teams", "duplas"),
            ("registrations", "inscripciones"),
            ("zone_teams", "zonas"),
            ("matches", "partidos"),
            ("winners", "resultados a favor"),
        )
        if holder.get(key)
    )
    if moved:
        messages.info(request, f"Datos transferidos: {moved}.")
    return redirect("panel_players", slug=tournament.slug)


@login_required
@require_POST
def panel_player_delete(request, slug, player_id):
    tournament = _tournament_or_404(request, slug)
    player = get_object_or_404(Player, pk=player_id)
    player_name = str(player)

    def action():
        players_service.delete_player(player)

    if not _run_action(request, action, f"«{player_name}» se eliminó de la base de jugadores."):
        return redirect("panel_player_edit", slug=tournament.slug, player_id=player.pk)
    return redirect("panel_players", slug=tournament.slug)


# ---------------------------------------------------------------------------
# Registro sin Google: solicitud pública de cuenta
# ---------------------------------------------------------------------------

_SIGNUP_REQUEST_OK = "¡Listo! Te vamos a avisar cuando tu cuenta esté creada."


def signup_request(request):
    """Página pública de «Registrarse»: pide los datos de la persona y su complejo.

    La solicitud queda visible en Configuración general para que la
    administración general verifique los datos y cree el usuario.
    """
    if request.method == "POST":
        if (request.POST.get("website") or "").strip():
            # Honeypot: sólo los bots completan el campo oculto.
            messages.success(request, _SIGNUP_REQUEST_OK)
            return redirect("signup_request")
        email = (request.POST.get("email") or "").strip().lower()
        first_name = (request.POST.get("first_name") or "").strip()
        last_name = (request.POST.get("last_name") or "").strip()
        phone = (request.POST.get("phone") or "").strip()
        complex_name = (request.POST.get("complex_name") or "").strip()
        complex_city = (request.POST.get("complex_city") or "").strip()
        complex_address = (request.POST.get("complex_address") or "").strip()
        try:
            validate_email(email)
            email_ok = True
        except ValidationError:
            email_ok = False
        if not (first_name and last_name and phone and complex_name):
            messages.error(
                request,
                "Completá tu nombre, apellido, teléfono y el nombre del complejo.",
            )
        elif not email_ok:
            messages.error(request, "Escribí un email válido así podemos avisarte.")
        elif UserModel.objects.filter(email__iexact=email).exists():
            messages.error(
                request,
                "Ese email ya tiene una cuenta: entrá con Google o pedile la contraseña a un administrador.",
            )
        elif RegistrationRequest.objects.filter(email__iexact=email).exists():
            messages.info(request, _SIGNUP_REQUEST_OK)
        else:
            request_row = RegistrationRequest.objects.create(
                email=email,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                complex_name=complex_name,
                complex_city=complex_city,
                complex_address=complex_address,
            )
            notify_new_signup_request(
                request_row, request.build_absolute_uri(reverse("general_settings"))
            )
            messages.success(request, _SIGNUP_REQUEST_OK)
        return redirect("signup_request")
    return render(request, "account/signup_request.html")


# ---------------------------------------------------------------------------
# Configuración general: usuarios del panel y accesos por complejo
# ---------------------------------------------------------------------------

UserModel = get_user_model()


def _require_settings_access(request):
    if not (request.user.is_staff or request.user.is_superuser):
        raise PermissionDenied("La configuración general es para usuarios del panel.")
    return request.user


def _manageable_complexes(user):
    if user.is_superuser:
        return Complex.objects.all()
    return user.complexes.all()


def _manageable_users(user):
    if user.is_superuser:
        return UserModel.objects.all()
    return (
        UserModel.objects.filter(Q(complexes__in=user.complexes.all()) | Q(pk=user.pk))
        .exclude(is_superuser=True)
        .distinct()
    )


def _can_manage_user(requester, target) -> bool:
    if requester.is_superuser:
        return True
    if target.is_superuser:
        return False
    if target.pk == requester.pk:
        return True
    return target.complexes.filter(pk__in=requester.complexes.values_list("pk", flat=True)).exists()


def _parse_complex_selection(request, requester, *, required=True) -> tuple[list[int], set[int]]:
    manageable_ids = set(_manageable_complexes(requester).values_list("id", flat=True))
    parsed: list[int] = []
    for raw in request.POST.getlist("complexes"):
        if not str(raw).isdigit():
            raise ValidationError("La selección de complejos no es válida.")
        parsed.append(int(raw))
    if set(parsed) - manageable_ids:
        raise ValidationError("Sólo podés asignar complejos que administrás.")
    if required and not parsed and not requester.is_superuser:
        raise ValidationError("Elegí al menos un complejo para el usuario.")
    return parsed, manageable_ids


def _create_complex(creator, *, name: str, city: str = "", address: str = "", phone: str = "", email: str = "") -> Complex:
    """Crea un complejo con slug único y deja a `creator` a cargo."""
    from django.utils.text import slugify

    base = slugify(name) or "complejo"
    slug_value = base
    counter = 2
    while Complex.objects.filter(slug=slug_value).exists():
        slug_value = f"{base}-{counter}"
        counter += 1
    complex_obj = Complex(
        name=name, slug=slug_value, city=city, address=address, phone=phone, email=email
    )
    complex_obj.full_clean()
    complex_obj.save()
    # Quien lo crea queda a cargo y puede gestionar sus torneos y usuarios.
    complex_obj.users.add(creator)
    return complex_obj


@login_required
def general_settings(request):
    account = _require_settings_access(request)
    query = (request.GET.get("q") or "").strip()
    prefill = {
        "email": (request.GET.get("email") or "").strip(),
        "first_name": (request.GET.get("first_name") or "").strip(),
        "last_name": (request.GET.get("last_name") or "").strip(),
        "phone": (request.GET.get("phone") or "").strip(),
        "username": "",
        "complex_name": (request.GET.get("complex_name") or "").strip(),
        "complex_city": (request.GET.get("complex_city") or "").strip(),
        "complex_address": (request.GET.get("complex_address") or "").strip(),
    }
    if prefill["email"]:
        # Sugerencia editable a partir del email (lo que va antes de la arroba).
        prefill["username"] = prefill["email"].split("@")[0][:150]
    users = _manageable_users(account).prefetch_related("complexes").order_by("username")
    if query:
        users = users.filter(
            Q(username__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
        )
    manageable_complexes = list(_manageable_complexes(account).order_by("name"))
    for complex_obj in manageable_complexes:
        # No usar annotate(Count("users")): el M2M ya viene unido por el usuario
        # que consulta y contaría sólo su propia fila.
        complex_obj.user_count = complex_obj.users.count()
        complex_obj.court_count = complex_obj.courts.count()
    rows = [
        {
            "account": entry,
            "is_self": entry.pk == account.pk,
            "complexes": list(entry.complexes.all()),
        }
        for entry in users
    ]
    invitations = []
    invitations_used = []
    registration_requests = []
    if account.is_superuser:
        invitations = list(
            Invitation.objects.filter(used_at__isnull=True).select_related("complex", "created_by")
        )
        invitations_used = list(
            Invitation.objects.filter(used_at__isnull=False)
            .select_related("complex", "used_by")
            .order_by("-used_at")[:10]
        )
        registration_requests = list(RegistrationRequest.objects.all())
    return render(
        request,
        "panel/settings.html",
        {
            "rows": rows,
            "query": query,
            "complexes": manageable_complexes,
            "can_grant_superuser": account.is_superuser,
            "preselect_complex": manageable_complexes[0].pk if len(manageable_complexes) == 1 else None,
            "is_general_admin": account.is_superuser,
            "invitations": invitations,
            "invitations_used": invitations_used,
            "registration_requests": registration_requests,
            "prefill": prefill,
        },
    )


def _maybe_send_credentials(request, account, password, *, notify: bool) -> bool:
    """Envía por email el usuario y la contraseña temporal, si se pidió."""
    if not notify:
        return False
    if not account.email:
        messages.warning(request, "No se envió el email: el usuario no tiene un email cargado.")
        return False
    if not password:
        messages.info(
            request,
            "Para enviar los datos de acceso tenés que cargar una contraseña nueva.",
        )
        return False
    body = (
        f"Hola {account.get_full_name() or account.username}:\n\n"
        "Ya está creada tu cuenta en Pádel Server.\n\n"
        f"Usuario: {account.username}\n"
        f"Contraseña temporal: {password}\n\n"
        "Entrá desde "
        f"{request.build_absolute_uri(reverse('account_login'))}\n"
        "y cambiá la contraseña por una tuya: el panel te lo va a recordar al ingresar.\n\n"
        "Si no esperabas este mensaje, ignoralo."
    )
    try:
        send_mail(
            "Tu cuenta de Pádel Server está lista",
            body,
            settings.DEFAULT_FROM_EMAIL,
            [account.email],
            fail_silently=False,
        )
    except Exception as exc:
        messages.warning(
            request,
            f"El usuario se guardó, pero el email no se pudo enviar: {exc}",
        )
        return False
    messages.success(request, f"Se enviaron los datos de acceso a {account.email}.")
    return True


@login_required
@require_POST
def general_user_create(request):
    requester = _require_settings_access(request)
    holder: dict = {}

    def action():
        username = (request.POST.get("username") or "").strip()
        if not username:
            raise ValidationError("Elegí un nombre de usuario.")
        password = request.POST.get("password") or ""
        if password != (request.POST.get("password2") or ""):
            raise ValidationError("Las contraseñas no coinciden.")
        create_complex = request.POST.get("create_complex") == "1"
        complex_name = (request.POST.get("complex_name") or "").strip()
        new_complex_requested = create_complex and bool(complex_name)
        complex_ids, _ = _parse_complex_selection(
            request, requester, required=not new_complex_requested
        )
        account = UserModel(
            username=username,
            first_name=(request.POST.get("first_name") or "").strip(),
            last_name=(request.POST.get("last_name") or "").strip(),
            email=(request.POST.get("email") or "").strip(),
            is_staff=request.POST.get("is_staff") == "1",
            is_superuser=bool(requester.is_superuser and request.POST.get("is_superuser") == "1"),
            is_active=True,
        )
        validate_password(password, account)
        account.set_password(password)
        account.full_clean()
        account.save()
        if new_complex_requested:
            new_complex = _create_complex(
                requester,
                name=complex_name,
                city=(request.POST.get("complex_city") or "").strip(),
                address=(request.POST.get("complex_address") or "").strip(),
                phone=(request.POST.get("complex_phone") or "").strip(),
                email=(request.POST.get("complex_email") or "").strip(),
            )
            complex_ids.append(new_complex.pk)
        account.complexes.set(complex_ids)
        holder["account"] = account
        holder["password"] = password

    ok = _run_action(request, action, "Usuario creado.")
    if not ok:
        return redirect("general_settings")
    account = holder["account"]
    TemporaryPassword.objects.update_or_create(user=account)
    if account.email:
        RegistrationRequest.objects.filter(email__iexact=account.email).delete()
    sent = _maybe_send_credentials(
        request, account, holder["password"], notify=request.POST.get("notify_email") == "1"
    )
    if not sent:
        messages.info(
            request,
            "Guardá la contraseña temporal en un lugar seguro y pedile al usuario que la cambie al ingresar.",
        )
    return redirect("general_user_edit", user_id=account.pk)


@login_required
def general_user_edit(request, user_id):
    requester = _require_settings_access(request)
    account = get_object_or_404(UserModel, pk=user_id)
    if not _can_manage_user(requester, account):
        raise PermissionDenied("No podés administrar ese usuario.")
    is_self = account.pk == requester.pk

    if request.method == "POST":
        holder: dict = {}

        def action():
            username = (request.POST.get("username") or "").strip()
            if not username:
                raise ValidationError("Elegí un nombre de usuario.")
            account.username = username
            account.first_name = (request.POST.get("first_name") or "").strip()
            account.last_name = (request.POST.get("last_name") or "").strip()
            account.email = (request.POST.get("email") or "").strip()
            password = request.POST.get("password") or ""
            if password:
                if password != (request.POST.get("password2") or ""):
                    raise ValidationError("Las contraseñas no coinciden.")
                validate_password(password, account)
                account.set_password(password)
                holder["password"] = password
            if is_self:
                # Sin auto-bloqueo: uno no puede desactivarse ni quitarse permisos.
                account.is_active = True
            else:
                account.is_active = request.POST.get("is_active") == "1"
                account.is_staff = request.POST.get("is_staff") == "1"
                if requester.is_superuser:
                    account.is_superuser = request.POST.get("is_superuser") == "1"
            # Los complejos se pueden cambiar también al editar el propio usuario:
            # así el administrador general puede sumarse un complejo recién creado.
            had_complexes = account.complexes.exists()
            complex_ids, manageable_ids = _parse_complex_selection(request, requester, required=False)
            outside_ids = list(
                account.complexes.exclude(pk__in=manageable_ids).values_list("pk", flat=True)
            )
            if not complex_ids and not outside_ids and not account.is_superuser:
                # Si ya no tenía complejos, no se lo obliga a sumarse uno para
                # poder editar sus datos; si tenía, no puede quedar sin ninguno.
                if not (is_self and not had_complexes):
                    if is_self:
                        raise ValidationError("Tenés que quedar a cargo de al menos un complejo.")
                    raise ValidationError("Elegí al menos un complejo para el usuario.")
            if is_self and not account.is_superuser:
                # Misma regla que «Dejar de administrar»: no se puede dejar un
                # complejo sin nadie a cargo desde la autoedición.
                for owned in account.complexes.filter(pk__in=manageable_ids):
                    if (
                        owned.pk not in complex_ids
                        and not owned.users.exclude(pk=account.pk).exists()
                    ):
                        raise ValidationError(
                            f"Sos la única persona a cargo de «{owned.name}». "
                            "Si no lo vas a usar más, eliminalo."
                        )
            account.complexes.set(list(complex_ids) + outside_ids)
            account.full_clean()
            account.save()

        if _run_action(request, action, f"Usuario «{account.username}» actualizado."):
            new_password = holder.get("password")
            if new_password:
                if is_self:
                    # Se la cambió el propio usuario: ya no es temporal.
                    TemporaryPassword.objects.filter(user=account).delete()
                    # Sin esto, Django invalida la sesión actual al cambiar la
                    # contraseña y el usuario queda deslogueado.
                    update_session_auth_hash(request, account)
                else:
                    TemporaryPassword.objects.update_or_create(user=account)
                    _maybe_send_credentials(
                        request,
                        account,
                        new_password,
                        notify=request.POST.get("notify_email") == "1",
                    )
            return redirect("general_user_edit", user_id=account.pk)

    complex_ids = set(account.complexes.values_list("pk", flat=True))
    return render(
        request,
        "panel/settings_user_form.html",
        {
            "account": account,
            "is_self": is_self,
            "can_grant_superuser": requester.is_superuser and not is_self,
            "can_delete_user": requester.is_superuser and not is_self,
            "complexes": list(_manageable_complexes(requester)),
            "account_complex_ids": complex_ids,
        },
    )


@login_required
@require_POST
def general_user_toggle_active(request, user_id):
    """Suspende o reactiva una cuenta sin borrar sus datos.

    Pensado para licencias impagas: el usuario no puede ingresar, pero sus
    complejos, torneos y reservas quedan intactos para cuando se regularice.
    """
    requester = _require_settings_access(request)
    account = get_object_or_404(UserModel, pk=user_id)
    if not _can_manage_user(requester, account):
        raise PermissionDenied("No podés administrar ese usuario.")
    if account.pk == requester.pk:
        messages.error(request, "No podés suspender tu propia cuenta.")
    elif account.is_superuser and not requester.is_superuser:
        raise PermissionDenied("Sólo otro administrador general puede suspender a un administrador general.")
    else:
        account.is_active = not account.is_active
        account.save(update_fields=["is_active"])
        if account.is_active:
            messages.success(
                request, f"Usuario «{account.username}» reactivado: ya puede volver a ingresar."
            )
        else:
            messages.success(
                request,
                f"Usuario «{account.username}» suspendido: no puede ingresar, pero sus datos, "
                "complejos y torneos se conservan.",
            )
    if request.POST.get("volver") == "editar":
        return redirect("general_user_edit", user_id=account.pk)
    return redirect("general_settings")


def _user_complexes_split(account) -> tuple[list[Complex], list[Complex]]:
    """Separa los complejos propios (único usuario) de los compartidos."""
    owned: list[Complex] = []
    shared: list[Complex] = []
    for complex_obj in account.complexes.all():
        if complex_obj.users.exclude(pk=account.pk).exists():
            shared.append(complex_obj)
        else:
            owned.append(complex_obj)
    return owned, shared


def _complex_delete_stats(complex_obj: Complex) -> dict:
    return {
        "tournaments": complex_obj.tournaments.count(),
        "courts": complex_obj.courts.count(),
        "bookings": CourtBooking.objects.filter(complex=complex_obj).count(),
        "users": complex_obj.users.count(),
    }


def _delete_complex(complex_obj: Complex) -> None:
    """Elimina el complejo con sus torneos, canchas y reservas.

    Los jugadores y las duplas de la base compartida no se tocan: se reutilizan
    en otros complejos y torneos.
    """
    with transaction.atomic():
        complex_obj.tournaments.all().delete()
        complex_obj.courts.all().delete()
        complex_obj.delete()


@login_required
def general_user_delete(request, user_id):
    """Elimina una cuenta y los complejos de los que es único responsable."""
    requester = _require_settings_access(request)
    if not requester.is_superuser:
        raise PermissionDenied("Sólo el administrador general puede eliminar usuarios.")
    account = get_object_or_404(UserModel, pk=user_id)
    if account.pk == requester.pk:
        messages.error(request, "No podés eliminar tu propia cuenta.")
        return redirect("general_settings")

    owned, shared = _user_complexes_split(account)
    if request.method == "POST":
        typed = (request.POST.get("confirm_username") or "").strip()
        if typed.casefold() != (account.username or "").strip().casefold():
            messages.error(
                request, "Escribí el nombre de usuario exacto para confirmar la eliminación."
            )
        else:
            username = account.username
            owned_names = [complex_obj.name for complex_obj in owned]
            shared_names = [complex_obj.name for complex_obj in shared]
            with transaction.atomic():
                for complex_obj in owned:
                    _delete_complex(complex_obj)
                for complex_obj in shared:
                    complex_obj.users.remove(account)
                account.delete()
            messages.success(request, f"Usuario «{username}» eliminado definitivamente.")
            if owned_names:
                messages.info(
                    request,
                    "Se eliminaron sus complejos: " + ", ".join(owned_names)
                    + " (con sus torneos, canchas y reservas).",
                )
            if shared_names:
                messages.info(
                    request,
                    "Quedaron sin este usuario porque los comparte con otras personas: "
                    + ", ".join(shared_names) + ".",
                )
            messages.info(
                request, "Los jugadores y duplas de la base compartida se conservaron."
            )
            return redirect("general_settings")

    return render(
        request,
        "panel/settings_user_delete.html",
        {
            "account": account,
            "owned_complexes": [
                {"complex": complex_obj, "stats": _complex_delete_stats(complex_obj)}
                for complex_obj in owned
            ],
            "shared_complexes": shared,
            "owned_tournaments": sum(
                _complex_delete_stats(complex_obj)["tournaments"] for complex_obj in owned
            ),
        },
    )


def _manageable_complex(requester, complex_id: int) -> Complex:
    complex_obj = get_object_or_404(Complex, pk=complex_id)
    if not requester.is_superuser and not requester.complexes.filter(pk=complex_obj.pk).exists():
        raise PermissionDenied("No podés administrar ese complejo.")
    return complex_obj


@login_required
@require_POST
def general_complex_create(request):
    requester = _require_settings_access(request)

    def action():
        name = (request.POST.get("name") or "").strip()
        if not name:
            raise ValidationError("Elegí un nombre para el complejo.")
        _create_complex(
            requester,
            name=name,
            city=(request.POST.get("city") or "").strip(),
            address=(request.POST.get("address") or "").strip(),
            phone=(request.POST.get("phone") or "").strip(),
            email=(request.POST.get("email") or "").strip(),
        )

    _run_action(request, action, "Complejo creado. Ya podés armar torneos ahí.")
    return redirect("general_settings")


@login_required
def general_complex_edit(request, complex_id):
    requester = _require_settings_access(request)
    complex_obj = _manageable_complex(requester, complex_id)
    if request.method == "POST":

        def action():
            name = (request.POST.get("name") or "").strip()
            if not name:
                raise ValidationError("Elegí un nombre para el complejo.")
            complex_obj.name = name
            complex_obj.city = (request.POST.get("city") or "").strip()
            complex_obj.address = (request.POST.get("address") or "").strip()
            complex_obj.phone = (request.POST.get("phone") or "").strip()
            complex_obj.email = (request.POST.get("email") or "").strip()
            complex_obj.full_clean()
            complex_obj.save()

        if _run_action(request, action, "Datos del complejo actualizados."):
            return redirect("general_settings")
    courts = list(complex_obj.courts.annotate(booking_count=Count("bookings")).order_by("name"))
    return render(
        request,
        "panel/settings_complex_form.html",
        {"complex": complex_obj, "courts": courts, "surfaces": Court.SURFACE},
    )


def _court_or_404(requester, court_id: int) -> Court:
    """Devuelve la cancha si el usuario puede administrarla."""
    court = get_object_or_404(Court, pk=court_id)
    if court.complex_id:
        _manageable_complex(requester, court.complex_id)
    elif not requester.is_superuser:
        raise PermissionDenied("No podés administrar esa cancha.")
    return court


def _court_surface(raw: str) -> str:
    value = (raw or "").strip() or "artificial"
    if value not in {key for key, _ in Court.SURFACE}:
        raise ValidationError("Elegí una superficie válida para la cancha.")
    return value


def _validate_court_name(complex_obj: Complex, name: str, *, exclude_pk: int | None = None) -> None:
    duplicates = complex_obj.courts.filter(name__iexact=name)
    if exclude_pk:
        duplicates = duplicates.exclude(pk=exclude_pk)
    if duplicates.exists():
        raise ValidationError("Ya hay una cancha con ese nombre en el complejo.")


def _court_return_url(court: Court) -> str:
    if court.complex_id:
        return reverse("general_complex_edit", args=[court.complex_id]) + "#canchas"
    return reverse("general_settings")


@login_required
@require_POST
def general_court_create(request, complex_id):
    """Agrega una cancha al complejo desde «Mis complejos»."""
    requester = _require_settings_access(request)
    complex_obj = _manageable_complex(requester, complex_id)
    name = (request.POST.get("name") or "").strip()

    def action():
        if not name:
            raise ValidationError("Elegí un nombre para la cancha.")
        _validate_court_name(complex_obj, name)
        court = Court(
            complex=complex_obj,
            name=name,
            surface=_court_surface(request.POST.get("surface") or ""),
            is_active=True,
        )
        court.full_clean()
        court.save()

    _run_action(
        request,
        action,
        f"Cancha «{name}» agregada. Si está activa, ya aparece en la reserva de turnos.",
    )
    return redirect(reverse("general_complex_edit", args=[complex_obj.pk]) + "#canchas")


@login_required
def general_court_edit(request, court_id):
    requester = _require_settings_access(request)
    court = _court_or_404(requester, court_id)
    if request.method == "POST":

        def action():
            name = (request.POST.get("name") or "").strip()
            if not name:
                raise ValidationError("Elegí un nombre para la cancha.")
            if court.complex_id:
                _validate_court_name(court.complex, name, exclude_pk=court.pk)
            court.name = name
            court.surface = _court_surface(request.POST.get("surface") or "")
            court.is_active = request.POST.get("is_active") == "1"
            court.full_clean()
            court.save()

        if _run_action(request, action, "Cancha actualizada."):
            return redirect(_court_return_url(court))
    return render(
        request,
        "panel/settings_court_form.html",
        {"court": court, "surfaces": Court.SURFACE},
    )


@login_required
@require_POST
def general_court_toggle(request, court_id):
    requester = _require_settings_access(request)
    court = _court_or_404(requester, court_id)
    court.is_active = not court.is_active
    court.save(update_fields=["is_active"])
    if court.is_active:
        messages.success(
            request,
            f"Cancha «{court.name}» activada: ya aparece en la reserva de turnos.",
        )
    else:
        messages.success(
            request,
            f"Cancha «{court.name}» desactivada: no se ofrece en la reserva pública.",
        )
    return redirect(_court_return_url(court))



@login_required
def general_complex_delete(request, complex_id):
    """Elimina un complejo con sus torneos, canchas y reservas.

    Un usuario común sólo puede eliminar complejos de los que es único
    responsable; los compartidos los elimina el administrador general (o puede
    dejar de administrarlos). El nombre exacto se usa como confirmación.
    """
    requester = _require_settings_access(request)
    complex_obj = _manageable_complex(requester, complex_id)
    other_users = complex_obj.users.exclude(pk=requester.pk)
    can_delete = requester.is_superuser or not other_users.exists()
    if request.method == "POST":
        if not can_delete:
            messages.error(
                request,
                "Ese complejo tiene otros usuarios asignados. Pedile al administrador general "
                "que lo elimine o usá «Dejar de administrar».",
            )
        else:
            typed = (request.POST.get("confirm_name") or "").strip()
            if typed.casefold() != complex_obj.name.strip().casefold():
                messages.error(
                    request, "Escribí el nombre del complejo exacto para confirmar la eliminación."
                )
            else:
                name = complex_obj.name
                _delete_complex(complex_obj)
                messages.success(
                    request,
                    f"Complejo «{name}» eliminado con sus torneos, canchas y reservas.",
                )
                messages.info(
                    request, "Los jugadores y duplas de la base compartida se conservaron."
                )
                return redirect("general_settings")
    return render(
        request,
        "panel/settings_complex_delete.html",
        {
            "complex": complex_obj,
            "stats": _complex_delete_stats(complex_obj),
            "other_users": other_users,
            "can_delete": can_delete,
        },
    )


@login_required
@require_POST
def general_complex_leave(request, complex_id):
    """Quita al usuario del complejo sin borrar los datos compartidos."""
    requester = _require_settings_access(request)
    complex_obj = _manageable_complex(requester, complex_id)
    if requester.is_superuser:
        messages.error(
            request,
            "El administrador general no deja de administrar complejos: si no lo usás más, eliminalo.",
        )
        return redirect("general_settings")
    if not complex_obj.users.exclude(pk=requester.pk).exists():
        messages.error(
            request,
            "Sos la única persona a cargo de ese complejo. Si no lo vas a usar más, eliminalo.",
        )
        return redirect("general_settings")
    complex_obj.users.remove(requester)
    messages.success(
        request,
        f"Dejaste de administrar «{complex_obj.name}». El complejo sigue a cargo del resto de los usuarios.",
    )
    return redirect("general_settings")


@login_required
@require_POST
def general_invitation_create(request):
    requester = _require_settings_access(request)
    if not requester.is_superuser:
        raise PermissionDenied("Sólo el administrador general gestiona invitaciones.")

    def action():
        email = (request.POST.get("email") or "").strip().lower()
        if not email:
            raise ValidationError("Cargá el email de Google de la persona a invitar.")
        try:
            validate_email(email)
        except ValidationError:
            raise ValidationError("El email no tiene un formato válido.")
        if UserModel.objects.filter(email__iexact=email).exists():
            raise ValidationError(
                "Ya existe un usuario con ese email. Editalo desde la lista de usuarios para darle acceso."
            )
        if Invitation.find_pending(email):
            raise ValidationError("Ese email ya tiene una invitación pendiente.")
        complex_obj = None
        complex_value = (request.POST.get("complex") or "").strip()
        if complex_value:
            if not complex_value.isdigit():
                raise ValidationError("El complejo elegido no es válido.")
            complex_obj = Complex.objects.filter(pk=int(complex_value)).first()
            if complex_obj is None:
                raise ValidationError("El complejo elegido no existe.")
        invitation = Invitation(email=email, complex=complex_obj, created_by=requester)
        invitation.save()

    _run_action(
        request,
        action,
        "Invitación guardada: cuando ingrese con Google se crea la cuenta del invitado.",
    )
    return redirect("general_settings")


@login_required
@require_POST
def general_invitation_delete(request, invitation_id):
    requester = _require_settings_access(request)
    if not requester.is_superuser:
        raise PermissionDenied("Sólo el administrador general gestiona invitaciones.")
    invitation = get_object_or_404(Invitation, pk=invitation_id)
    if invitation.used_at is None:
        invitation.delete()
        messages.success(request, "Invitación revocada.")
    return redirect("general_settings")


@login_required
@require_POST
def general_registration_request_delete(request, request_id):
    requester = _require_settings_access(request)
    if not requester.is_superuser:
        raise PermissionDenied("Sólo el administrador general gestiona solicitudes.")
    registration = get_object_or_404(RegistrationRequest, pk=request_id)
    registration.delete()
    messages.success(request, "Solicitud descartada.")
    return redirect("general_settings")


# ---------------------------------------------------------------------------
# Zonas
# ---------------------------------------------------------------------------


@login_required
def panel_zones(request, slug):
    tournament = _tournament_or_404(request, slug)
    zones = []
    for zone in tournament.zones.all().order_by("order", "name"):
        rows = draws.basic_zone_standings(zone)
        zones.append(
            {
                "zone": zone,
                "standings": rows,
                "complete": draws.is_zone_complete(zone),
                "pending": zone.matches.filter(stage="group").filter(winner__isnull=True).count(),
            }
        )
    teams = list(players_service.tournament_teams(tournament))
    teams_without_zone = [
        team
        for team in teams
        if not ZoneTeam.objects.filter(zone__tournament=tournament, team=team).exists()
    ]
    context = _panel_context(
        request,
        tournament,
        "zonas",
        zone_rows=zones,
        teams_without_zone=teams_without_zone,
        can_build_zones=len(teams) >= 2,
    )
    return render(request, "panel/zones.html", context)


@login_required
@require_POST
def panel_zones_generate(request, slug):
    tournament = _tournament_or_404(request, slug)

    def action():
        players_service.generate_zones(tournament, actor=request.user)

    _run_action(request, action, "Zonas generadas y partidos programados automáticamente.")
    return redirect("panel_zones", slug=tournament.slug)


@login_required
def panel_zones_manual(request, slug):
    tournament = _tournament_or_404(request, slug)
    teams = list(players_service.tournament_teams(tournament))
    if len(teams) < 2:
        messages.error(request, "Necesitás al menos 2 duplas inscriptas para armar zonas.")
        return redirect("panel_zones", slug=tournament.slug)
    zones = list(tournament.zones.order_by("order", "name"))
    zone_options = [{"value": f"z:{zone.pk}", "label": f"Zona {zone.name}"} for zone in zones]
    new_slot_count = max(0, len(teams) // 2 - len(zones))
    new_options = [
        {"value": f"new:{slot}", "label": f"Zona nueva {slot}"}
        for slot in range(1, new_slot_count + 1)
    ]
    max_size = max(2, tournament.teams_per_zone)

    current: dict[int, int] = {}
    for assignment in ZoneTeam.objects.filter(zone__tournament=tournament):
        current.setdefault(assignment.team_id, assignment.zone_id)

    if request.method == "POST":
        posted = {
            team.pk: (request.POST.get(f"team_{team.pk}") or "").strip() for team in teams
        }

        def action():
            players_service.save_manual_zones(tournament, posted)

        if _run_action(request, action, "Zonas guardadas: quedaron como las armaste."):
            return redirect("panel_zones", slug=tournament.slug)
        rows = [{"team": team, "value": posted[team.pk]} for team in teams]
    else:
        rows = [
            {"team": team, "value": f"z:{current[team.pk]}" if team.pk in current else ""}
            for team in teams
        ]

    context = _panel_context(
        request,
        tournament,
        "zonas",
        rows=rows,
        zone_options=zone_options,
        new_options=new_options,
        max_size=max_size,
        bracket_blocked=players_service.bracket_has_real_data(tournament),
    )
    return render(request, "panel/zones_manual.html", context)


@login_required
@require_POST
def panel_zone_assign(request, slug):
    tournament = _tournament_or_404(request, slug)
    zone = get_object_or_404(Zone, pk=request.POST.get("zone"), tournament=tournament)
    team = get_object_or_404(Team, pk=request.POST.get("team"))

    def action():
        players_service.assign_team_to_zone(zone, team)

    _run_action(request, action, f"{team} asignada a la zona {zone.name}.")
    return redirect("panel_zones", slug=tournament.slug)


@login_required
@require_POST
def panel_zone_remove_team(request, slug, zone_id, team_id):
    tournament = _tournament_or_404(request, slug)
    zone = get_object_or_404(Zone, pk=zone_id, tournament=tournament)
    team = get_object_or_404(Team, pk=team_id)

    def action():
        players_service.remove_team_from_zone(zone, team)

    _run_action(request, action, f"{team} quitada de la zona {zone.name}.")
    return redirect("panel_zones", slug=tournament.slug)


# ---------------------------------------------------------------------------
# Partidos
# ---------------------------------------------------------------------------


@login_required
def panel_matches(request, slug):
    tournament = _tournament_or_404(request, slug)
    zones = list(tournament.zones.all().order_by("order", "name"))
    group_rows = []
    for zone in zones:
        group_rows.append(
            {
                "zone": zone,
                "matches": list(zone.matches.filter(stage="group").select_related("team1", "team2", "winner").order_by("slot", "id")),
            }
        )
    playoff_rows = draws.bracket_rounds(tournament)
    zone_map = [
        {
            "id": zone.pk,
            "name": zone.name,
            "teams": [{"id": team.pk, "label": str(team)} for team in zone.teams.select_related("player1", "player2")],
        }
        for zone in zones
    ]
    context = _panel_context(
        request,
        tournament,
        "partidos",
        group_rows=group_rows,
        playoff_rows=playoff_rows,
        zone_map=zone_map,
    )
    return render(request, "panel/matches.html", context)


@login_required
@require_POST
def panel_match_create(request, slug):
    tournament = _tournament_or_404(request, slug)
    zone = get_object_or_404(Zone, pk=request.POST.get("zone"), tournament=tournament)
    team1 = get_object_or_404(Team, pk=request.POST.get("team1"))
    team2 = get_object_or_404(Team, pk=request.POST.get("team2"))

    def action():
        matches_service.create_group_match(tournament, zone, team1, team2)

    _run_action(request, action, "Partido de zona agregado.")
    return redirect("panel_matches", slug=tournament.slug)


@login_required
@require_POST
def panel_match_delete(request, slug, match_id):
    tournament = _tournament_or_404(request, slug)
    match = get_object_or_404(Match, pk=match_id, tournament=tournament)

    def action():
        matches_service.delete_group_match(match)

    _run_action(request, action, "Partido eliminado.")
    return redirect("panel_matches", slug=tournament.slug)


# ---------------------------------------------------------------------------
# Resultados
# ---------------------------------------------------------------------------


def _pending_needs(match: Match) -> list[dict]:
    """Explica qué ganadores alimentan cada posición de un partido de llave."""
    needs = []
    feeders = sorted(match.feeders.all(), key=lambda row: (row.next_slot or 0, row.id))
    for feeder in feeders:
        needs.append(
            {
                "label": f"Ganador de {feeder.display_stage} {feeder.slot}",
                "winner": feeder.winner,
                "defined": feeder.winner_id is not None,
            }
        )
    return needs


def _result_rows(tournament):
    """Devuelve (grupos_pendientes, jugados).

    ``grupos_pendientes`` es una lista de ``{"label": ..., "rows": [...]}`` con
    los partidos pendientes ordenados por ronda real (zonas primero, luego la
    llave ronda a ronda) para que el administrador vea el camino completo hasta
    la final. Cada fila incluye ``needs``: los cruces de los que depende.
    """
    group_pending = list(
        Match.objects.filter(tournament=tournament, stage="group", winner__isnull=True, is_walkover=False)
        .select_related("team1", "team2", "zone")
        .order_by("zone__order", "zone__name", "slot", "id")
    )
    playoff_pending = list(
        Match.objects.filter(
            tournament=tournament,
            stage__in=PLAYOFF_STAGES,
            is_bye=False,
            winner__isnull=True,
            is_walkover=False,
        )
        .select_related("team1", "team2")
        .prefetch_related("feeders__team1", "feeders__team2")
        .order_by("round_number", "slot", "id")
    )

    pending_groups: list[dict] = []
    current: dict | None = None
    for match in group_pending:
        label = f"Zona {match.zone.name}"
        if current is None or current["label"] != label:
            current = {"label": label, "rows": []}
            pending_groups.append(current)
        current["rows"].append({"match": match, "needs": []})
    for match in playoff_pending:
        label = match.display_stage
        if current is None or current["label"] != label:
            current = {"label": label, "rows": []}
            pending_groups.append(current)
        needs = [] if (match.team1_id and match.team2_id) else _pending_needs(match)
        current["rows"].append({"match": match, "needs": needs})

    played = list(
        Match.objects.filter(tournament=tournament)
        .exclude(is_bye=True)
        .filter(Q(winner__isnull=False) | Q(is_walkover=True))
        .select_related("team1", "team2", "zone", "winner", "recorded_by")
        .order_by("-played_at", "-id")[:50]
    )
    return pending_groups, played


@login_required
def panel_results(request, slug):
    tournament = _tournament_or_404(request, slug)
    pending_groups, played = _result_rows(tournament)
    pending_total = sum(len(group["rows"]) for group in pending_groups)
    waiting_total = sum(
        1 for group in pending_groups for row in group["rows"] if not (row["match"].team1_id and row["match"].team2_id)
    )
    context = _panel_context(
        request,
        tournament,
        "resultados",
        pending_groups=pending_groups,
        pending_total=pending_total,
        waiting_total=waiting_total,
        played=played,
    )
    return render(request, "panel/results.html", context)


@login_required
@require_POST
def panel_result_register(request, slug, match_id):
    tournament = _tournament_or_404(request, slug)
    match = get_object_or_404(Match, pk=match_id, tournament=tournament)
    is_walkover = request.POST.get("is_walkover") in {"1", "true", "on"}
    score = (request.POST.get("score") or "").strip()
    winner = None
    winner_id = request.POST.get("winner")
    if winner_id:
        winner = get_object_or_404(Team, pk=winner_id)
    reason = request.POST.get("walkover_reason", "")

    def action():
        if is_walkover and winner is None:
            raise ValidationError("El walkover requiere indicar el ganador.")
        updated = register_match_result(
            match,
            score="" if is_walkover else score,
            winner=winner,
            is_walkover=is_walkover,
            walkover_reason=reason if is_walkover else "",
            played_at=None,
            actor=request.user,
        )
        if not is_walkover and score and (updated.score or "").strip() != score:
            messages.info(
                request,
                f"El marcador se guardó acomodado a la pareja ganadora elegida: {updated.score}.",
            )

    if _run_action(request, action, "Resultado registrado y llave actualizada."):
        return redirect("panel_results", slug=tournament.slug)
    return redirect("panel_results", slug=tournament.slug)


# ---------------------------------------------------------------------------
# Clasificación
# ---------------------------------------------------------------------------


@login_required
def panel_standings(request, slug):
    tournament = _tournament_or_404(request, slug)
    rows = []
    for zone in tournament.zones.all().order_by("order", "name"):
        complete = draws.is_zone_complete(zone)
        if complete:
            standings = draws.official_zone_standings(zone)
        else:
            standings = draws.basic_zone_standings(zone)
        rows.append({"zone": zone, "standings": standings, "official": complete})
    qualified = []
    error = None
    if rows and all(item["official"] for item in rows):
        try:
            qualified = draws.qualified_teams_detailed(tournament)
        except draws.BracketError as exc:
            error = _error_text(exc)
    context = _panel_context(
        request,
        tournament,
        "clasificacion",
        rows=rows,
        qualified=qualified,
        standings_error=error,
    )
    return render(request, "panel/standings.html", context)


# ---------------------------------------------------------------------------
# Llaves
# ---------------------------------------------------------------------------


@login_required
def panel_bracket(request, slug):
    tournament = _tournament_or_404(request, slug)
    rounds = draws.bracket_rounds(tournament)
    zones = list(tournament.zones.all().order_by("order", "name"))
    zones_complete = bool(zones) and all(draws.is_zone_complete(zone) for zone in zones)
    qualifiers_total = len(zones) * tournament.qualifiers_per_zone
    has_real_data = any(
        match.has_real_data
        for match in Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES)
    )
    vista = request.GET.get("vista") or "cuadro"
    if vista not in ("cuadro", "detalle"):
        vista = "cuadro"

    # Clasificadas por zona (para el cuadro gráfico): zona y posición de cada dupla.
    zone_origin: dict[int, str] = {}
    zone_qualifiers: list[dict] = []
    if rounds:
        try:
            detailed = draws.qualified_teams_detailed(tournament)
        except draws.BracketError:
            detailed = []
        by_zone: dict = {}
        for item in detailed:
            by_zone.setdefault(item["zone"], []).append(item)
            zone_origin[item["team"].id] = f"Zona {item['zone'].name} · {item['zone_position']}°"
        zone_qualifiers = [
            {"zone": zone, "entries": sorted(entries, key=lambda entry: entry["zone_position"])}
            for zone, entries in sorted(by_zone.items(), key=lambda item: (item[0].order, item[0].name))
        ]

    tree = [
        {
            "label": round_data["label"],
            "matches": [
                {
                    "match": match,
                    "origin1": zone_origin.get(match.team1_id, "") if round_data["depth"] == 1 else "",
                    "origin2": zone_origin.get(match.team2_id, "") if round_data["depth"] == 1 else "",
                }
                for match in round_data["matches"]
            ],
        }
        for round_data in rounds
    ]

    context = _panel_context(
        request,
        tournament,
        "llaves",
        rounds=rounds,
        tree=tree,
        zone_qualifiers=zone_qualifiers,
        vista=vista,
        zones_complete=zones_complete,
        qualifiers_total=qualifiers_total,
        pending_zones=draws.pending_zone_summary(tournament),
        bracket_exists=bool(rounds),
        has_real_data=has_real_data,
        manual=bool(tournament.manual_bracket),
        can_build=(
            tournament.playoffs_enabled
            and zones_complete
            and qualifiers_total >= 2
            and not has_real_data
        ),
    )
    return render(request, "panel/bracket.html", context)


@login_required
@require_POST
def panel_bracket_generate(request, slug):
    tournament = _tournament_or_404(request, slug)

    def action():
        # Siembra automática explícita: reemplaza también un armado manual
        # mientras no haya resultados reales cargados.
        draws.generate_playoff_bracket(tournament)

    _run_action(request, action, "Llaves generadas con siembra automática.")
    return redirect("panel_bracket", slug=tournament.slug)


def _slot_value(raw) -> int | str:
    """Normaliza un valor de cruce para el formulario: id numérico o vacío."""
    if raw is not None and str(raw).isdigit():
        return int(raw)
    return ""


def _manual_bracket_slots(tournament: Tournament, matches_count: int, pool_ids: set[int]) -> list[dict]:
    """Cruces de primera ronda actuales, listos para el formulario manual."""
    current = {row.slot: row for row in draws.first_round_matches(tournament)}
    slots = []
    for slot in range(1, matches_count + 1):
        row = current.get(slot)
        team1 = row.team1_id if row and row.team1_id in pool_ids else None
        team2 = row.team2_id if row and row.team2_id in pool_ids else None
        slots.append({"slot": slot, "team1": team1 or "", "team2": team2 or ""})
    return slots


@login_required
def panel_bracket_manual(request, slug):
    tournament = _tournament_or_404(request, slug)
    try:
        info = draws.playoff_bracket_info(tournament)
    except draws.BracketError as exc:
        messages.error(request, _error_text(exc))
        return redirect("panel_bracket", slug=tournament.slug)

    has_real_data = any(
        match.has_real_data
        for match in Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES)
    )
    if has_real_data:
        messages.error(
            request,
            "La llave ya tiene resultados cargados. Corregí o eliminá esos partidos antes de rearmar los cruces.",
        )
        return redirect("panel_bracket", slug=tournament.slug)

    pool_ids = {entry["team"].pk for entry in info["detailed"]}
    if request.method == "POST":
        selections = [
            (
                request.POST.get(f"team1_{slot}") or None,
                request.POST.get(f"team2_{slot}") or None,
            )
            for slot in range(1, info["matches"] + 1)
        ]

        def action():
            draws.generate_playoff_bracket_manual(tournament, selections)

        if _run_action(request, action, "Cruces guardados: la llave quedó armada a tu manera."):
            return redirect("panel_bracket", slug=tournament.slug)
        slots = [
            {"slot": index + 1, "team1": _slot_value(raw[0]), "team2": _slot_value(raw[1])}
            for index, raw in enumerate(selections)
        ]
    else:
        slots = _manual_bracket_slots(tournament, info["matches"], pool_ids)

    by_zone: dict = {}
    for item in info["detailed"]:
        by_zone.setdefault(item["zone"], []).append(item)
    pool_zones = [
        {"zone": zone, "entries": sorted(entries, key=lambda entry: entry["zone_position"])}
        for zone, entries in sorted(by_zone.items(), key=lambda item: (item[0].order, item[0].name))
    ]
    pool = [entry for group in pool_zones for entry in group["entries"]]

    context = _panel_context(
        request,
        tournament,
        "llaves",
        pool=pool,
        pool_zones=pool_zones,
        slots=slots,
        matches_count=info["matches"],
        bracket_size=info["p"],
        byes=info["byes"],
    )
    return render(request, "panel/bracket_manual.html", context)


# ---------------------------------------------------------------------------
# API interna
# ---------------------------------------------------------------------------


@login_required
def zone_teams_json(request, zone_id):
    zone = get_object_or_404(Zone.objects.select_related("tournament"), pk=zone_id)
    if not access.user_can_access_tournament(request.user, zone.tournament):
        raise PermissionDenied("No tenés acceso a este torneo.")
    results = [
        {"id": team.pk, "text": str(team)}
        for team in zone.teams.select_related("player1", "player2")
    ]
    return JsonResponse({"results": results})


@login_required
def tournament_players_json(request, slug):
    tournament = _tournament_or_404(request, slug)
    results = [
        {"id": player.pk, "text": str(player)}
        for player in players_service.available_players(tournament)
    ]
    return JsonResponse({"results": results})


# ---------------------------------------------------------------------------
# Vistas públicas
# ---------------------------------------------------------------------------


def public_tournaments(request):
    tournaments = (
        Tournament.objects.filter(status__in=["open", "running", "finished"])
        .select_related("complex")
        .order_by("-start_date", "name")
    )
    return render(request, "tournaments_list.html", {"tournaments": tournaments, "public_section": "torneos"})


def public_tournament_detail(request, slug):
    tournament = get_object_or_404(Tournament.objects.select_related("complex"), slug=slug)
    zones = []
    for zone in tournament.zones.all().order_by("order", "name"):
        zones.append(
            {
                "zone": zone,
                "matches": list(
                    zone.matches.filter(stage="group")
                    .select_related("team1__player1", "team1__player2", "team2__player1", "team2__player2", "winner", "court")
                    .order_by("slot", "id")
                ),
            }
        )

    rounds = draws.bracket_rounds(tournament)

    # Origen de cada clasificada («Zona A · 1°») para la primera ronda del cuadro.
    zone_origin: dict[int, str] = {}
    if rounds:
        try:
            detailed = draws.qualified_teams_detailed(tournament)
        except draws.BracketError:
            detailed = []
        for item in detailed:
            zone_origin[item["team"].id] = f"Zona {item['zone'].name} · {item['zone_position']}°"

    tree = [
        {
            "label": round_data["label"],
            "matches": [
                {
                    "match": match,
                    "origin1": zone_origin.get(match.team1_id, "") if round_data["depth"] == 1 else "",
                    "origin2": zone_origin.get(match.team2_id, "") if round_data["depth"] == 1 else "",
                }
                for match in round_data["matches"]
            ],
        }
        for round_data in rounds
    ]

    return render(
        request,
        "tournament_detail.html",
        {
            "tournament": tournament,
            "zones": zones,
            "tree": tree,
            "signup_open": _signup_open(tournament),
        },
    )


# ---------------------------------------------------------------------------
# Autoinscripción pública de parejas (código del complejo)
# ---------------------------------------------------------------------------

_SIGNUP_OK = "¡Listo! Tu pareja quedó anotada y pendiente de confirmación del complejo."
_SIGNUP_FAIL_LIMIT = 15
_SIGNUP_FAIL_WINDOW = 900  # segundos


def _signup_open(tournament: Tournament) -> bool:
    return bool(
        tournament.status == "open"
        and tournament.self_registration_enabled
        and tournament.registration_code
    )


def _signup_attempts_key(request) -> str:
    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    ip = forwarded or request.META.get("REMOTE_ADDR", "local")
    return f"signup-code-fail:{ip}"


def _register_signup_failure(request) -> None:
    key = _signup_attempts_key(request)
    current = int(cache.get(key, 0) or 0)
    cache.set(key, current + 1, _SIGNUP_FAIL_WINDOW)


def public_tournament_signup(request, slug):
    """Formulario público para que una pareja se anote con el código del torneo."""
    tournament = get_object_or_404(Tournament.objects.select_related("complex"), slug=slug)
    if not _signup_open(tournament):
        messages.info(
            request,
            "La autoinscripción de este torneo no está abierta. Consultá con el complejo.",
        )
        return redirect("public_tournament_detail", slug=tournament.slug)

    entered = {"code": (request.GET.get("codigo") or "").strip().upper()}
    if request.method == "POST":
        entered = {
            "code": (request.POST.get("code") or "").strip().upper(),
            "player1_first_name": (request.POST.get("player1_first_name") or "").strip(),
            "player1_last_name": (request.POST.get("player1_last_name") or "").strip(),
            "player1_gender": request.POST.get("player1_gender", ""),
            "player1_phone": (request.POST.get("player1_phone") or "").strip(),
            "player2_first_name": (request.POST.get("player2_first_name") or "").strip(),
            "player2_last_name": (request.POST.get("player2_last_name") or "").strip(),
            "player2_gender": request.POST.get("player2_gender", ""),
            "player2_phone": (request.POST.get("player2_phone") or "").strip(),
        }
        if (request.POST.get("website") or "").strip():
            # Honeypot: sólo los bots completan el campo oculto.
            messages.success(request, _SIGNUP_OK)
            return redirect("public_tournament_detail", slug=tournament.slug)
        if int(cache.get(_signup_attempts_key(request), 0) or 0) >= _SIGNUP_FAIL_LIMIT:
            messages.error(
                request, "Demasiados intentos con códigos incorrectos. Probá de nuevo en un rato."
            )
        elif entered["code"] != tournament.registration_code:
            _register_signup_failure(request)
            messages.error(request, "El código no es correcto. Pedíselo de nuevo al complejo.")
        elif not all(
            entered[key]
            for key in (
                "player1_first_name",
                "player1_last_name",
                "player2_first_name",
                "player2_last_name",
            )
        ):
            messages.error(request, "Completá el nombre y apellido de los dos jugadores.")
        else:
            try:
                with transaction.atomic():
                    player1, _ = players_service.resolve_player(
                        f"{entered['player1_last_name']}, {entered['player1_first_name']}",
                        gender=entered["player1_gender"],
                        level=tournament.category,
                    )
                    player2, _ = players_service.resolve_player(
                        f"{entered['player2_last_name']}, {entered['player2_first_name']}",
                        gender=entered["player2_gender"],
                        level=tournament.category,
                    )
                    for player, phone in (
                        (player1, entered["player1_phone"]),
                        (player2, entered["player2_phone"]),
                    ):
                        if phone and not player.phone:
                            player.phone = phone
                            player.save(update_fields=["phone"])
                    team = players_service.create_team(
                        tournament,
                        player1,
                        player2,
                        registration_status="pending",
                        source="public",
                    )
            except ValidationError as exc:
                messages.error(request, _error_text(exc))
            else:
                cache.delete(_signup_attempts_key(request))
                notify_tournament_team_signup(
                    tournament,
                    team,
                    [entered["player1_phone"], entered["player2_phone"]],
                    request.build_absolute_uri(reverse("panel_teams", kwargs={"slug": tournament.slug})),
                )
                messages.success(request, _SIGNUP_OK)
                return redirect("public_tournament_detail", slug=tournament.slug)

    return render(
        request,
        "tournament_signup.html",
        {"tournament": tournament, "entered": entered, "genders": Player.GENDER},
    )


# ---------------------------------------------------------------------------
# Turnos de cancha (público)
# ---------------------------------------------------------------------------

TURNOS_DIAS = 60


def _parse_date(raw, default=None):
    if raw:
        try:
            return date_cls.fromisoformat(raw)
        except (TypeError, ValueError):
            pass
    return default


def _parse_month(raw, default=None):
    if raw:
        try:
            return date_cls.fromisoformat(f"{raw.strip()}-01")
        except (TypeError, ValueError):
            pass
    return default


def _parse_time(raw):
    if raw:
        try:
            return time_cls.fromisoformat(raw)
        except (TypeError, ValueError):
            pass
    return None


def _wa_number(phone: str) -> str:
    """Número para wa.me: sin símbolos y con 549 si viene sin código de país."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = "549" + digits
    return digits


def _wa_url(phone: str, text: str) -> str:
    digits = _wa_number(phone)
    if not digits:
        return ""
    return f"https://wa.me/{digits}?text={quote(text)}"


def _selected_public_complex(request):
    # El público solo ve complejos que pueden recibir reservas (con canchas activas).
    complexes = list(
        Complex.objects.filter(is_active=True, courts__is_active=True).distinct().order_by("name")
    )
    slug = request.GET.get("complejo") or ""
    selected = next((item for item in complexes if item.slug == slug), None)
    if selected is None and complexes:
        selected = complexes[0]
    return complexes, selected


def turnos(request):
    """Grilla pública de turnos con reserva sin registro (calendario + horarios)."""
    today = timezone.localdate()
    complexes, selected = _selected_public_complex(request)

    last_day = today + timedelta(days=TURNOS_DIAS - 1)
    day = _parse_date(request.GET.get("fecha"), today)
    if day < today:
        day = today
    if day > last_day:
        day = last_day

    board = schedule_service.board_for_day(selected, day) if selected else None

    # Calendario del mes visible (por defecto, el del día elegido).
    calendar_data = None
    if selected:
        month = _parse_month(request.GET.get("mes"), day.replace(day=1))
        first_month = today.replace(day=1)
        last_month = last_day.replace(day=1)
        if month < first_month:
            month = first_month
        if month > last_month:
            month = last_month
        weeks = schedule_service.month_calendar(
            selected, month, today=today, last_day=last_day, selected=day
        )
        calendar_data = {
            "month": month,
            "weeks": weeks,
            "prev": month - timedelta(days=1) if month > first_month else None,
            "next": (month + timedelta(days=32)).replace(day=1) if month < last_month else None,
        }

    selected_slot = None
    if board:
        court_id = request.GET.get("cancha")
        start = _parse_time(request.GET.get("hora"))
        if court_id and start:
            court = next((item for item in board["courts"] if str(item.pk) == str(court_id)), None)
            bounds = schedule_service.slot_for(selected, day, start)
            taken = court is not None and any(
                booking.court_id == court.pk and booking.start_time == start for booking in board["bookings"]
            )
            if court and bounds and not taken and not schedule_service.is_slot_past(day, start):
                selected_slot = {"court": court, "start": bounds[0], "end": bounds[1]}

    return render(
        request,
        "turnos.html",
        {
            "public_section": "turnos",
            "complexes": complexes,
            "selected_complex": selected,
            "day": day,
            "today": today,
            "last_day": last_day,
            "calendar": calendar_data,
            "board": board,
            "selected_slot": selected_slot,
        },
    )


@require_POST
def turno_reservar(request):
    """Reserva pública: valida la grilla del día y crea el turno."""
    complex_obj = get_object_or_404(Complex, slug=(request.POST.get("complejo") or ""), is_active=True)
    day = _parse_date(request.POST.get("fecha"))
    court = Court.objects.filter(
        pk=request.POST.get("cancha") or 0, complex=complex_obj, is_active=True
    ).first()
    start = _parse_time(request.POST.get("hora"))
    name = (request.POST.get("nombre") or "").strip()
    phone = (request.POST.get("telefono") or "").strip()
    note = (request.POST.get("nota") or "").strip()

    back = f"{reverse('turnos')}?complejo={complex_obj.slug}"
    if day is not None:
        back += f"&fecha={day.isoformat()}"

    today = timezone.localdate()
    if day is None or court is None or start is None:
        messages.error(request, "No pudimos leer los datos del turno. Probá de nuevo.")
        return redirect(back)
    if day < today or day > today + timedelta(days=TURNOS_DIAS - 1):
        messages.error(request, "Elegí una fecha dentro de los próximos días.")
        return redirect(back)
    if not name:
        messages.error(request, "Completá tu nombre y apellido.")
        return redirect(back)
    if len(phone) < 6:
        messages.error(request, "Completá un WhatsApp de contacto.")
        return redirect(back)
    bounds = schedule_service.slot_for(complex_obj, day, start)
    if bounds is None:
        messages.error(request, "Ese horario ya no está disponible.")
        return redirect(back)
    if schedule_service.is_slot_past(day, start):
        messages.error(request, "Ese turno ya pasó. Elegí otro horario.")
        return redirect(back)

    try:
        with transaction.atomic():
            booking = CourtBooking.objects.create(
                complex=complex_obj,
                court=court,
                date=day,
                start_time=bounds[0],
                end_time=bounds[1],
                status="booked",
                customer_name=name[:80],
                customer_phone=phone[:40],
                note=note[:140],
                price=complex_obj.booking_price,
                source="public",
            )
    except IntegrityError:
        messages.error(request, "Ese horario acaba de ser reservado por otra persona. Elegí otro.")
        return redirect(back)

    notify_court_booking(booking)
    messages.success(request, "¡Turno confirmado! Te esperamos.")
    return redirect("turno_confirmado", code=booking.code)


def turno_confirmado(request, code):
    booking = get_object_or_404(CourtBooking.objects.select_related("complex", "court"), code=code.upper())
    whatsapp_url = ""
    if booking.complex.whatsapp_phone and booking.status == "booked":
        whatsapp_url = _wa_url(
            booking.complex.whatsapp_phone,
            (
                f"Hola, confirmo mi turno {booking.code} en {booking.complex.name}: "
                f"{booking.court.name}, {booking.date:%d/%m/%Y} de "
                f"{booking.start_time:%H:%M} a {booking.end_time:%H:%M}. "
                f"({booking.customer_name})"
            ),
        )
    return render(
        request,
        "turno_confirmado.html",
        {
            "public_section": "turnos",
            "booking": booking,
            "whatsapp_url": whatsapp_url,
            "can_cancel": booking.status == "booked"
            and not schedule_service.is_slot_past(booking.date, booking.start_time),
        },
    )


def turno_cancelar(request, code):
    booking = get_object_or_404(CourtBooking.objects.select_related("complex", "court"), code=code.upper())
    can_cancel = booking.status == "booked" and not schedule_service.is_slot_past(
        booking.date, booking.start_time
    )
    if request.method == "POST":
        if booking.status != "booked":
            messages.info(request, "Ese turno ya no está activo.")
        elif not can_cancel:
            messages.error(request, "El turno ya pasó y no se puede cancelar.")
        else:
            booking.status = "cancelled"
            booking.cancelled_at = timezone.now()
            booking.save(update_fields=["status", "cancelled_at"])
            messages.success(request, "Turno cancelado. El horario vuelve a estar libre.")
        return redirect("turno_confirmado", code=booking.code)
    return render(
        request,
        "turno_cancelar.html",
        {"public_section": "turnos", "booking": booking, "can_cancel": can_cancel},
    )


# ---------------------------------------------------------------------------
# Turnos de cancha (panel)
# ---------------------------------------------------------------------------


def _turnos_complex(request):
    complexes = list(_manageable_complexes(request.user).filter(is_active=True).order_by("name"))
    slug = request.GET.get("complejo") or request.POST.get("complejo") or ""
    selected = next((item for item in complexes if item.slug == slug), None)
    if selected is None and complexes:
        selected = complexes[0]
    return complexes, selected


@login_required
def panel_turnos(request):
    """Agenda del día: ocupación, carga manual, bloqueos, cobros y cancelaciones."""
    complexes, selected = _turnos_complex(request)
    today = timezone.localdate()
    day = _parse_date(request.GET.get("fecha"), today)
    board = schedule_service.board_for_day(selected, day) if selected else None
    bookings = (
        schedule_service.bookings_for_day(selected, day, include_cancelled=True) if selected else []
    )

    day_total = Decimal("0")
    day_paid = Decimal("0")
    for booking in bookings:
        if booking.status == "cancelled":
            continue
        day_total += booking.price or Decimal("0")
        if booking.is_paid:
            day_paid += booking.price or Decimal("0")
        if booking.customer_phone and booking.status == "booked":
            booking.wa_url = _wa_url(
                booking.customer_phone,
                (
                    f"Hola {booking.customer_name}, te confirmamos el turno en {booking.complex.name}: "
                    f"{booking.court.name}, {booking.date:%d/%m} de "
                    f"{booking.start_time:%H:%M} a {booking.end_time:%H:%M}."
                ),
            )
        else:
            booking.wa_url = ""

    return render(
        request,
        "panel/turnos.html",
        {
            "complexes": complexes,
            "selected_complex": selected,
            "day": day,
            "today": today,
            "prev_day": day - timedelta(days=1),
            "next_day": day + timedelta(days=1),
            "board": board,
            "bookings": bookings,
            "day_total": day_total,
            "day_paid": day_paid,
        },
    )


@login_required
def panel_turnos_horarios(request):
    """Configuración de horarios por tipo de día, pausas y fechas especiales."""
    complexes, selected = _turnos_complex(request)
    rows = []
    overrides = []
    if selected:
        schedules = {schedule.day_type: schedule for schedule in selected.court_schedules.all()}
        for key, label in CourtSchedule.DAY_TYPE:
            schedule = schedules.get(key)
            rows.append(
                {
                    "key": key,
                    "label": label,
                    "schedule": schedule,
                    "is_open": schedule.is_open if schedule else True,
                    "start_time": schedule.start_time if schedule else schedule_service.DEFAULT_START,
                    "end_time": schedule.end_time if schedule else schedule_service.DEFAULT_END,
                    "slot_minutes": schedule.slot_minutes if schedule else schedule_service.DEFAULT_SLOT_MINUTES,
                    "pauses": list(schedule.pauses.all()) if schedule else [],
                }
            )
        overrides = list(
            selected.court_overrides.filter(date__gte=timezone.localdate() - timedelta(days=1))
            .order_by("date")[:90]
        )
    return render(
        request,
        "panel/turnos_horarios.html",
        {
            "complexes": complexes,
            "selected_complex": selected,
            "day_type_rows": rows,
            "overrides": overrides,
        },
    )


@login_required
@require_POST
def panel_turno_accion(request):
    complex_obj = get_object_or_404(Complex, slug=(request.POST.get("complejo") or ""))
    access.require_complex_access(request.user, complex_obj)
    action = request.POST.get("accion")
    day = _parse_date(request.POST.get("fecha"))
    back = f"{reverse('panel_turnos')}?complejo={complex_obj.slug}"
    if day is not None:
        back += f"&fecha={day.isoformat()}"

    if action in ("marcar_pagado", "marcar_pendiente"):
        booking = get_object_or_404(CourtBooking, pk=request.POST.get("turno_id") or 0, complex=complex_obj)
        booking.is_paid = action == "marcar_pagado"
        booking.save(update_fields=["is_paid"])
        messages.success(
            request,
            "Turno marcado como abonado." if booking.is_paid else "Turno marcado como pendiente de pago.",
        )
        return redirect(back)

    if action == "cancelar":
        booking = get_object_or_404(CourtBooking, pk=request.POST.get("turno_id") or 0, complex=complex_obj)
        if booking.status in ("booked", "blocked"):
            booking.status = "cancelled"
            booking.cancelled_at = timezone.now()
            booking.save(update_fields=["status", "cancelled_at"])
            messages.success(request, "Turno cancelado; el horario quedó libre.")
        else:
            messages.info(request, "Ese turno ya estaba cancelado.")
        return redirect(back)

    if action not in ("reservar", "bloquear"):
        messages.error(request, "Acción desconocida.")
        return redirect(back)
    if day is None:
        messages.error(request, "Elegí una fecha válida.")
        return redirect(back)

    court = Court.objects.filter(
        pk=request.POST.get("cancha") or 0, complex=complex_obj, is_active=True
    ).first()
    start = _parse_time(request.POST.get("hora"))
    if court is None or start is None:
        messages.error(request, "Elegí la cancha y el horario.")
        return redirect(back)
    bounds = schedule_service.slot_for(complex_obj, day, start)
    if bounds is None:
        messages.error(request, "Ese horario no existe en la grilla del día.")
        return redirect(back)

    name = (request.POST.get("nombre") or "").strip()
    phone = (request.POST.get("telefono") or "").strip()
    note = (request.POST.get("nota") or "").strip()
    if action == "reservar" and not name:
        messages.error(request, "Para reservar cargá el nombre del cliente.")
        return redirect(back)

    try:
        with transaction.atomic():
            CourtBooking.objects.create(
                complex=complex_obj,
                court=court,
                date=day,
                start_time=bounds[0],
                end_time=bounds[1],
                status="booked" if action == "reservar" else "blocked",
                customer_name=name[:80] if action == "reservar" else "",
                customer_phone=phone[:40] if action == "reservar" else "",
                note=note[:140],
                price=complex_obj.booking_price if action == "reservar" else 0,
                source="panel",
                created_by=request.user,
            )
    except IntegrityError:
        messages.error(request, "Ese horario ya está ocupado.")
        return redirect(back)

    messages.success(request, "Turno reservado." if action == "reservar" else "Horario bloqueado.")
    return redirect(back)


@login_required
def panel_turnos_ajustes(request):
    """Precio del turno, datos de cobro (Mercado Pago) y aviso por WhatsApp."""
    complexes, selected = _turnos_complex(request)
    return render(
        request,
        "panel/turnos_ajustes.html",
        {
            "complexes": complexes,
            "selected_complex": selected,
        },
    )


@login_required
@require_POST
def panel_horarios_accion(request):
    complex_obj = get_object_or_404(Complex, slug=(request.POST.get("complejo") or ""))
    access.require_complex_access(request.user, complex_obj)
    action = request.POST.get("accion")
    back = f"{reverse('panel_turnos_horarios')}?complejo={complex_obj.slug}"
    ajustes_back = f"{reverse('panel_turnos_ajustes')}?complejo={complex_obj.slug}"

    if action == "guardar_horario":
        day_type = request.POST.get("tipo_dia")
        if day_type not in dict(CourtSchedule.DAY_TYPE):
            messages.error(request, "Tipo de día inválido.")
            return redirect(back)
        schedule, _ = CourtSchedule.objects.get_or_create(complex=complex_obj, day_type=day_type)
        schedule.is_open = request.POST.get("abierto") == "on"
        schedule.start_time = _parse_time(request.POST.get("desde")) or schedule.start_time
        schedule.end_time = _parse_time(request.POST.get("hasta")) or schedule.end_time
        duration = request.POST.get("duracion")
        if duration:
            try:
                schedule.slot_minutes = int(duration)
            except ValueError:
                pass
        try:
            schedule.full_clean()
        except ValidationError as exc:
            messages.error(request, _error_text(exc))
            return redirect(back)
        schedule.save()
        messages.success(request, f"Horario guardado: {schedule.get_day_type_display()}.")

    elif action == "agregar_pausa":
        schedule = get_object_or_404(
            CourtSchedule, pk=request.POST.get("horario_id") or 0, complex=complex_obj
        )
        pause = SchedulePause(
            schedule=schedule,
            start_time=_parse_time(request.POST.get("desde")),
            end_time=_parse_time(request.POST.get("hasta")),
        )
        if pause.start_time is None or pause.end_time is None:
            messages.error(request, "Completá la hora de inicio y de fin de la pausa.")
        else:
            try:
                pause.full_clean()
            except ValidationError as exc:
                messages.error(request, _error_text(exc))
            else:
                pause.save()
                messages.success(request, "Pausa agregada.")

    elif action == "quitar_pausa":
        pause = get_object_or_404(
            SchedulePause, pk=request.POST.get("pausa_id") or 0, schedule__complex=complex_obj
        )
        pause.delete()
        messages.success(request, "Pausa eliminada.")

    elif action == "guardar_fecha":
        day = _parse_date(request.POST.get("fecha"))
        if day is None:
            messages.error(request, "Elegí una fecha válida.")
            return redirect(back)
        override, _ = CourtDateOverride.objects.get_or_create(complex=complex_obj, date=day)
        override.is_closed = request.POST.get("cerrado") == "on"
        override.start_time = _parse_time(request.POST.get("desde"))
        override.end_time = _parse_time(request.POST.get("hasta"))
        override.note = (request.POST.get("nota") or "").strip()[:120]
        try:
            override.full_clean()
        except ValidationError as exc:
            messages.error(request, _error_text(exc))
            return redirect(back)
        override.save()
        messages.success(request, "Fecha especial guardada.")

    elif action == "quitar_fecha":
        override = get_object_or_404(
            CourtDateOverride, pk=request.POST.get("fecha_id") or 0, complex=complex_obj
        )
        override.delete()
        messages.success(request, "Fecha especial eliminada.")

    elif action == "guardar_ajustes":
        price_raw = (request.POST.get("precio") or "").strip().replace(",", ".")
        try:
            price = Decimal(price_raw or "0")
        except InvalidOperation:
            price = Decimal("-1")
        if price < 0:
            messages.error(request, "El precio del turno no es válido.")
            return redirect(ajustes_back)
        mp_link = (request.POST.get("mp_link") or "").strip()
        if mp_link and not mp_link.startswith(("http://", "https://")):
            mp_link = "https://" + mp_link
        if mp_link:
            try:
                URLValidator()(mp_link)
            except ValidationError:
                messages.error(request, "El link de Mercado Pago no parece válido.")
                return redirect(ajustes_back)
        complex_obj.booking_price = price.quantize(Decimal("0.01"))
        complex_obj.mp_alias = (request.POST.get("mp_alias") or "").strip()[:80]
        complex_obj.mp_holder = (request.POST.get("mp_holder") or "").strip()[:80]
        complex_obj.mp_link = mp_link
        complex_obj.whatsapp_phone = (request.POST.get("whatsapp_phone") or "").strip()[:40]
        complex_obj.whatsapp_apikey = (request.POST.get("whatsapp_apikey") or "").strip()[:60]
        complex_obj.save(
            update_fields=[
                "booking_price",
                "mp_alias",
                "mp_holder",
                "mp_link",
                "whatsapp_phone",
                "whatsapp_apikey",
            ]
        )
        messages.success(request, "Ajustes del turnero guardados.")
        return redirect(ajustes_back)

    else:
        messages.error(request, "Acción desconocida.")

    return redirect(back)
