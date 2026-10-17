"""Vistas de Pádel Server.

- Panel de gestión propio (sin depender del admin de Django) con selección de
  torneo y secciones: resumen, parejas, zonas, partidos, resultados,
  clasificación y llaves.
- Vistas públicas de consulta.
- Toda operación valida permisos de complejo/torneo y reglas en el servidor.
"""

from __future__ import annotations

from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Count, ProtectedError, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core import access
from core.models import Complex, Invitation, Match, Player, Registration, RegistrationRequest, Team, TemporaryPassword, Tournament, TournamentPlayer, Zone, ZoneTeam
from core.notifications import notify_new_signup_request, notify_tournament_team_signup
from core.services import draws
from core.services import matches as matches_service
from core.services import players as players_service
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
    if request.user.is_authenticated:
        return redirect("tournament_select")
    return redirect("account_login")


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
    if not request.user.is_superuser and not complexes.exists():
        messages.error(
            request,
            "No tenés complejos asignados. Creá el tuyo desde Configuración general y volvé a intentar.",
        )
        return redirect("tournament_select")
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        start_date = request.POST.get("start_date") or None
        complex_id = request.POST.get("complex") or None
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
                        complex_obj = None
                    defaults = {
                        "complex": complex_obj,
                        "name": name,
                        "category": request.POST.get("category") or "principiante",
                        "start_date": start_date,
                        "end_date": request.POST.get("end_date") or None,
                        "format_type": request.POST.get("format_type") or "zonas_playoffs",
                        "best_of_sets": int(request.POST.get("best_of_sets") or 2),
                        "teams_per_zone": int(request.POST.get("teams_per_zone") or 3),
                        "qualifiers_per_zone": int(request.POST.get("qualifiers_per_zone") or 2),
                        "status": request.POST.get("status") or "draft",
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
    signup_url = request.build_absolute_uri(
        reverse("public_tournament_signup", kwargs={"slug": tournament.slug})
    )
    whatsapp_link_url = "https://wa.me/?text=" + quote(signup_url)
    whatsapp_hint_url = "https://wa.me/?text=" + quote("Copiá el siguiente código y anotá tu pareja 👇")
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
        whatsapp_link_url=whatsapp_link_url,
        whatsapp_hint_url=whatsapp_hint_url,
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
    manageable_complexes = list(_manageable_complexes(account))
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
                complex_ids, manageable_ids = _parse_complex_selection(request, requester, required=False)
                outside_ids = list(
                    account.complexes.exclude(pk__in=manageable_ids).values_list("pk", flat=True)
                )
                if not complex_ids and not outside_ids and not account.is_superuser:
                    raise ValidationError("Elegí al menos un complejo para el usuario.")
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
            "complexes": list(_manageable_complexes(requester)),
            "account_complex_ids": complex_ids,
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
    return render(
        request,
        "panel/settings_complex_form.html",
        {"complex": complex_obj},
    )


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
    return render(request, "tournaments_list.html", {"tournaments": tournaments})


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
