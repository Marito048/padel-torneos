from django.urls import include, path

from core import views

urlpatterns = [
    path("", views.home, name="home"),
    # Autenticación: cuentas existentes + login con Google (django-allauth).
    path("cuentas/", include("allauth.urls")),
    # Registro público sin Google: deja una solicitud con el email.
    path("registrarse/", views.signup_request, name="signup_request"),
    # Gestión.
    path("gestion/", views.tournament_select, name="tournament_select"),
    path("gestion/salir/", views.tournament_exit, name="tournament_exit"),
    path("gestion/nuevo/", views.tournament_form, name="tournament_create"),
    path("gestion/elegir/<slug:slug>/", views.tournament_choose, name="tournament_choose"),
    # Configuración general (usuarios del panel, complejos y accesos).
    path("gestion/configuracion/", views.general_settings, name="general_settings"),
    path("gestion/configuracion/usuarios/crear/", views.general_user_create, name="general_user_create"),
    path(
        "gestion/configuracion/usuarios/<int:user_id>/",
        views.general_user_edit,
        name="general_user_edit",
    ),
    path(
        "gestion/configuracion/usuarios/<int:user_id>/estado/",
        views.general_user_toggle_active,
        name="general_user_toggle_active",
    ),
    path(
        "gestion/configuracion/usuarios/<int:user_id>/eliminar/",
        views.general_user_delete,
        name="general_user_delete",
    ),
    path("gestion/configuracion/complejos/crear/", views.general_complex_create, name="general_complex_create"),
    path(
        "gestion/configuracion/complejos/<int:complex_id>/",
        views.general_complex_edit,
        name="general_complex_edit",
    ),
    path(
        "gestion/configuracion/complejos/<int:complex_id>/eliminar/",
        views.general_complex_delete,
        name="general_complex_delete",
    ),
    path(
        "gestion/configuracion/complejos/<int:complex_id>/dejar/",
        views.general_complex_leave,
        name="general_complex_leave",
    ),
    path(
        "gestion/configuracion/invitaciones/crear/",
        views.general_invitation_create,
        name="general_invitation_create",
    ),
    path(
        "gestion/configuracion/invitaciones/<int:invitation_id>/eliminar/",
        views.general_invitation_delete,
        name="general_invitation_delete",
    ),
    path(
        "gestion/configuracion/solicitudes/<int:request_id>/eliminar/",
        views.general_registration_request_delete,
        name="general_registration_request_delete",
    ),
    # Turnos de cancha (panel). Antes de gestion/<slug> para no chocar con ese patrón.
    path("gestion/turnos/", views.panel_turnos, name="panel_turnos"),
    path("gestion/turnos/horarios/", views.panel_turnos_horarios, name="panel_turnos_horarios"),
    path("gestion/turnos/accion/", views.panel_turno_accion, name="panel_turno_accion"),
    path("gestion/turnos/horarios/accion/", views.panel_horarios_accion, name="panel_horarios_accion"),
    path("gestion/turnos/ajustes/", views.panel_turnos_ajustes, name="panel_turnos_ajustes"),
    path("gestion/<slug:slug>/", views.tournament_manage, name="tournament_manage"),
    path("gestion/<slug:slug>/configuracion/", views.tournament_form, name="tournament_edit"),
    path("gestion/<slug:slug>/eliminar/", views.panel_tournament_delete, name="panel_tournament_delete"),
    path("gestion/<slug:slug>/parejas/", views.panel_teams, name="panel_teams"),
    path("gestion/<slug:slug>/parejas/crear/", views.panel_team_create, name="panel_team_create"),
    path("gestion/<slug:slug>/parejas/<int:team_id>/eliminar/", views.panel_team_delete, name="panel_team_delete"),
    path(
        "gestion/<slug:slug>/autoinscripcion/<str:action>/",
        views.panel_self_registration,
        name="panel_self_registration",
    ),
    path(
        "gestion/<slug:slug>/inscripciones/<int:registration_id>/<str:action>/",
        views.panel_registration_action,
        name="panel_registration_action",
    ),
    path("gestion/<slug:slug>/jugadores/", views.panel_players, name="panel_players"),
    path("gestion/<slug:slug>/jugadores/crear/", views.panel_player_create, name="panel_player_create"),
    path("gestion/<slug:slug>/jugadores/<int:player_id>/", views.panel_player_edit, name="panel_player_edit"),
    path("gestion/<slug:slug>/jugadores/<int:player_id>/unir/", views.panel_player_merge, name="panel_player_merge"),
    path("gestion/<slug:slug>/jugadores/<int:player_id>/eliminar/", views.panel_player_delete, name="panel_player_delete"),
    path("gestion/<slug:slug>/zonas/", views.panel_zones, name="panel_zones"),
    path("gestion/<slug:slug>/zonas/generar/", views.panel_zones_generate, name="panel_zones_generate"),
    path("gestion/<slug:slug>/zonas/manual/", views.panel_zones_manual, name="panel_zones_manual"),
    path("gestion/<slug:slug>/zonas/asignar/", views.panel_zone_assign, name="panel_zone_assign"),
    path(
        "gestion/<slug:slug>/zonas/<int:zone_id>/quitar/<int:team_id>/",
        views.panel_zone_remove_team,
        name="panel_zone_remove_team",
    ),
    path("gestion/<slug:slug>/partidos/", views.panel_matches, name="panel_matches"),
    path("gestion/<slug:slug>/partidos/crear/", views.panel_match_create, name="panel_match_create"),
    path("gestion/<slug:slug>/partidos/<int:match_id>/eliminar/", views.panel_match_delete, name="panel_match_delete"),
    path("gestion/<slug:slug>/resultados/", views.panel_results, name="panel_results"),
    path(
        "gestion/<slug:slug>/resultados/<int:match_id>/registrar/",
        views.panel_result_register,
        name="panel_result_register",
    ),
    path("gestion/<slug:slug>/clasificacion/", views.panel_standings, name="panel_standings"),
    path("gestion/<slug:slug>/llaves/", views.panel_bracket, name="panel_bracket"),
    path("gestion/<slug:slug>/llaves/generar/", views.panel_bracket_generate, name="panel_bracket_generate"),
    path("gestion/<slug:slug>/llaves/manual/", views.panel_bracket_manual, name="panel_bracket_manual"),
    # API interna del panel.
    path("gestion/api/zonas/<int:zone_id>/equipos/", views.zone_teams_json, name="zone_teams_json"),
    path("gestion/api/<slug:slug>/jugadores/", views.tournament_players_json, name="tournament_players_json"),
    # Consulta pública.
    path("torneos/", views.public_tournaments, name="public_tournaments"),
    path("torneos/<slug:slug>/", views.public_tournament_detail, name="public_tournament_detail"),
    path("torneos/<slug:slug>/anotarse/", views.public_tournament_signup, name="public_tournament_signup"),
    # Turnos de cancha (público, sin registro).
    path("turnos/", views.turnos, name="turnos"),
    path("turnos/reservar/", views.turno_reservar, name="turno_reservar"),
    path("turnos/confirmado/<str:code>/", views.turno_confirmado, name="turno_confirmado"),
    path("turnos/cancelar/<str:code>/", views.turno_cancelar, name="turno_cancelar"),
]

__all__ = ["urlpatterns"]
