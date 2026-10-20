"""Suite de pruebas de Pádel Server.

Cubre validación de resultados, walkovers, disponibilidad de jugadores con
garantía de base de datos, zonas, partidos, clasificación con desempates
auditables y generación/progresión automática de llaves con descansos.
"""

import re

from unittest.mock import patch
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialLogin

from core.adapters import PadelSocialAccountAdapter
from core.models import (
    Complex,
    Invitation,
    Match,
    Player,
    Registration,
    RegistrationRequest,
    Team,
    TemporaryPassword,
    TieBreakAudit,
    Tournament,
    TournamentPlayer,
    Zone,
    ZoneTeam,
)
from core.services import draws
from core.services import players as players_service
from core.services.draws import BracketConflictError, BracketError, has_super, parse_sets
from core.views import _SIGNUP_FAIL_LIMIT
from core.services.matches import create_group_match, delete_group_match
from core.services.tournament_workflow import register_match_result

PLAYOFF_STAGES = draws.PLAYOFF_STAGES


# ---------------------------------------------------------------------------
# Utilidades de fixture
# ---------------------------------------------------------------------------


class TournamentFixtureMixin:
    def make_tournament(
        self,
        slug="torneo-1",
        *,
        teams_per_zone=2,
        qualifiers_per_zone=2,
        best_of_sets=2,
        format_type="zonas_playoffs",
        complex_obj=None,
        name=None,
    ):
        return Tournament.objects.create(
            complex=complex_obj,
            name=name or f"Torneo {slug}",
            slug=slug,
            category="primera",
            start_date=timezone.localdate(),
            best_of_sets=best_of_sets,
            teams_per_zone=teams_per_zone,
            qualifiers_per_zone=qualifiers_per_zone,
            format_type=format_type,
            status="running",
        )

    def add_team(self, tournament, name=None):
        counter = Player.objects.count()
        p1 = Player.objects.create(first_name=f"P{counter}a", last_name="Test", gender="M")
        p2 = Player.objects.create(first_name=f"P{counter}b", last_name="Test", gender="M")
        team = Team.objects.create(player1=p1, player2=p2, name=name or "")
        Registration.objects.create(tournament=tournament, team=team, status="confirmed")
        return team

    def add_teams(self, tournament, count):
        return [self.add_team(tournament) for _ in range(count)]

    def make_zones(self, tournament, sizes):
        """Crea zonas con las duplas del torneo en el orden indicado y sus partidos."""
        zones = []
        teams = list(players_service.tournament_teams(tournament))
        index = 0
        for order, size in enumerate(sizes, start=1):
            zone = Zone.objects.create(tournament=tournament, name=chr(64 + order), order=order)
            for _ in range(size):
                ZoneTeam.objects.create(zone=zone, team=teams[index])
                index += 1
            players_service.ensure_zone_matches(zone)
            zones.append(zone)
        return zones

    def play_match(self, match, winner, score=None):
        if score is None:
            score = "6-4 6-3" if winner.pk == match.team1_id else "4-6 3-6"
        return register_match_result(match, score=score, winner=winner)

    def play_zone_group(self, zone, order=None):
        """Juega todos los partidos de una zona eligiendo ganadores deterministas."""
        matches = list(zone.matches.filter(stage="group").order_by("slot", "id"))
        winners = order or []
        for index, match in enumerate(matches):
            if index < len(winners):
                winner = winners[index]
            else:
                winner = match.team1
            self.play_match(match, winner)

    def completed_tournament(self, teams_count, *, qualifiers_per_zone=2, slug="bracket-t"):
        """Torneo con zonas de 2 duplas (última puede ser de 1) y fase de zonas cerrada."""
        tournament = self.make_tournament(slug=slug, qualifiers_per_zone=qualifiers_per_zone)
        self.add_teams(tournament, teams_count)
        sizes = [2] * (teams_count // 2)
        if teams_count % 2:
            sizes.append(1)
        zones = self.make_zones(tournament, sizes)
        for zone in zones:
            self.play_zone_group(zone)
        tournament.refresh_from_db()
        return tournament

    def play_playoffs(self, tournament):
        """Juega toda la llave disponible hasta que haya campeón."""
        guard = 0
        while guard < 200:
            guard += 1
            playable = list(
                Match.objects.filter(
                    tournament=tournament,
                    stage__in=PLAYOFF_STAGES,
                    is_bye=False,
                    winner__isnull=True,
                    team1__isnull=False,
                    team2__isnull=False,
                ).order_by("round_number", "slot")
            )
            if not playable:
                break
            for match in playable:
                self.play_match(match, match.team1)
        final = Match.objects.filter(tournament=tournament, stage="final").first()
        return final


# ---------------------------------------------------------------------------
# Resultados y marcadores
# ---------------------------------------------------------------------------


class ParseSetsTests(TestCase):
    def test_two_sets(self):
        self.assertEqual(parse_sets("6-4 4-6"), (1, 1, 10, 10))

    def test_super_tiebreak(self):
        self.assertEqual(parse_sets("6-4 4-6 [11-9]"), (1, 1, 10, 10))
        self.assertTrue(has_super("6-4 4-6 [11-9]"))
        self.assertEqual(parse_sets("6-4 4-6 11-9"), (1, 1, 10, 10))
        self.assertTrue(has_super("6-4 4-6 11-9"))
        self.assertFalse(has_super("6-4 6-3"))

    def test_zero_or_invalid_score(self):
        self.assertEqual(parse_sets(""), (0, 0, 0, 0))
        self.assertEqual(parse_sets("6-4 6-3 [x-y]"), (2, 0, 12, 7))


class MatchScoreValidationTests(TournamentFixtureMixin, TestCase):
    def setUp(self):
        self.tournament = self.make_tournament(slug="validacion", best_of_sets=2)
        self.team1, self.team2 = self.add_teams(self.tournament, 2)
        self.zone = self.make_zones(self.tournament, [2])[0]
        self.match = zone_match(self.zone)

    def test_valid_two_set_score_and_auto_winner(self):
        self.match.score = "6-4 6-3"
        self.match.save()
        self.assertEqual(self.match.winner_id, self.match.team1_id)
        self.assertEqual(self.match.status_label, "Finalizado")

    def test_valid_super_tiebreak(self):
        self.match.score = "6-4 4-6 [11-9]"
        self.match.save()
        self.assertEqual(self.match.winner_id, self.match.team1_id)
        self.assertTrue(self.match.decided_by_super)

    def test_plain_super_tiebreak_accepted(self):
        self.match.score = "6-4 4-6 11-9"
        self.match.save()
        self.assertEqual(self.match.winner_id, self.match.team1_id)
        self.assertTrue(self.match.decided_by_super)

    def test_plain_super_tiebreak_below_eleven_rejected(self):
        with self.assertRaises(ValidationError):
            self.match.score = "6-4 4-6 10-8"
            self.match.save()

    def test_plain_super_tiebreak_margin_rejected(self):
        with self.assertRaises(ValidationError):
            self.match.score = "6-4 4-6 11-10"
            self.match.save()

    def test_impossible_set_score_rejected(self):
        with self.assertRaises(ValidationError):
            self.match.score = "6-5 6-3"
            self.match.save()

    def test_tied_set_rejected(self):
        with self.assertRaises(ValidationError):
            self.match.score = "6-6 6-3"
            self.match.save()

    def test_missing_super_tiebreak_rejected(self):
        with self.assertRaises(ValidationError):
            self.match.score = "6-4 4-6"
            self.match.save()

    def test_impossible_super_tiebreak_rejected(self):
        with self.assertRaises(ValidationError):
            self.match.score = "6-4 4-6 [11-10]"
            self.match.save()

    def test_super_tiebreak_below_eleven_rejected(self):
        with self.assertRaises(ValidationError):
            self.match.score = "6-4 4-6 [10-8]"
            self.match.save()

    def test_winner_mismatch_rejected(self):
        self.match.score = "6-4 6-3"
        self.match.winner = self.team2
        with self.assertRaises(ValidationError):
            self.match.save()

    def test_incomplete_result_best_of_three_rejected(self):
        tournament = self.make_tournament(slug="bo3", best_of_sets=3)
        self.add_teams(tournament, 2)
        zone = self.make_zones(tournament, [2])[0]
        match = zone_match(zone)
        with self.assertRaises(ValidationError):
            match.score = "6-4"  # un solo set: mejor de 3 exige al menos 2
            match.save()
        self.assertIsNone(Match.objects.get(pk=match.pk).winner_id)
        with self.assertRaises(ValidationError):
            match.score = "4-6 6-4"  # empate de sets: falta tercer set o super tie-break
            match.save()
        self.assertIsNone(Match.objects.get(pk=match.pk).winner_id)

    def test_best_of_three_split_sets_decided_by_super(self):
        tournament = self.make_tournament(slug="bo3-super", best_of_sets=3)
        self.add_teams(tournament, 2)
        zone = self.make_zones(tournament, [2])[0]
        match = zone_match(zone)
        match.score = "6-4 4-6 [11-9]"
        match.save()
        self.assertEqual(match.winner_id, match.team1_id)
        self.assertTrue(match.decided_by_super)

    def test_best_of_three_plain_super_accepted(self):
        tournament = self.make_tournament(slug="bo3-super-plano", best_of_sets=3)
        self.add_teams(tournament, 2)
        zone = self.make_zones(tournament, [2])[0]
        match = zone_match(zone)
        match.score = "4-6 6-4 11-9"
        match.save()
        self.assertEqual(match.winner_id, match.team1_id)
        self.assertTrue(match.decided_by_super)

    def test_full_three_set_score_valid(self):
        tournament = self.make_tournament(slug="bo3-completo", best_of_sets=3)
        self.add_teams(tournament, 2)
        zone = self.make_zones(tournament, [2])[0]
        match = zone_match(zone)
        match.score = "6-4 4-6 7-5"
        match.save()
        self.assertEqual(match.winner_id, match.team1_id)

    def test_three_sets_for_best_of_two_rejected(self):
        with self.assertRaises(ValidationError):
            self.match.score = "6-4 4-6 6-3"
            self.match.save()

    def test_pending_match_has_no_winner(self):
        self.assertEqual(self.match.status_label, "Pendiente")

    def test_walkover_requires_winner_reason_and_no_score(self):
        with self.assertRaises(ValidationError):
            self.match.is_walkover = True
            self.match.walkover_reason = "lesión"
            self.match.save()
        self.match.is_walkover = False
        self.match.walkover_reason = ""

        with self.assertRaises(ValidationError):
            self.match.is_walkover = True
            self.match.winner = self.team1
            self.match.save()
        self.match.is_walkover = False
        self.match.winner = None

        with self.assertRaises(ValidationError):
            self.match.is_walkover = True
            self.match.winner = self.team1
            self.match.walkover_reason = "lesión"
            self.match.score = "6-0 6-0"
            self.match.save()
        self.match.is_walkover = False
        self.match.score = ""
        self.match.winner = None

    def test_valid_walkover_without_fake_score(self):
        self.match.is_walkover = True
        self.match.winner = self.team2
        self.match.walkover_reason = "ausencia"
        self.match.save()
        self.match.refresh_from_db()
        self.assertEqual(self.match.score, "")
        self.assertEqual(self.match.winner_id, self.team2.pk)
        self.assertEqual(self.match.status_label, "Walkover")


class WinnerReconciliationTests(TournamentFixtureMixin, TestCase):
    """El ganador elegido manda: el marcador espejado se acomoda solo."""

    def setUp(self):
        self.tournament = self.make_tournament(slug="ganador-elegido")
        self.teams = self.add_teams(self.tournament, 2)
        self.zone = self.make_zones(self.tournament, [2])[0]
        self.match = self.zone.matches.get(stage="group")

    def test_selected_winner_wins_with_mirrored_score(self):
        updated = register_match_result(self.match, score="6-3 6-2", winner=self.teams[1])
        self.assertEqual(updated.winner_id, self.teams[1].pk)
        self.assertEqual(updated.score, "3-6 2-6")

    def test_score_consistent_with_selected_winner_stays(self):
        updated = register_match_result(self.match, score="6-3 6-2", winner=self.teams[0])
        self.assertEqual(updated.winner_id, self.teams[0].pk)
        self.assertEqual(updated.score, "6-3 6-2")

    def test_super_tiebreak_is_mirrored_too(self):
        updated = register_match_result(
            self.match, score="6-3 3-6 [11-9]", winner=self.teams[1]
        )
        self.assertEqual(updated.winner_id, self.teams[1].pk)
        self.assertEqual(updated.score, "3-6 6-3 [9-11]")

    def test_plain_super_tiebreak_is_mirrored_too(self):
        updated = register_match_result(
            self.match, score="6-3 3-6 11-9", winner=self.teams[1]
        )
        self.assertEqual(updated.winner_id, self.teams[1].pk)
        self.assertEqual(updated.score, "3-6 6-3 9-11")

    def test_automatic_winner_follows_corrected_score(self):
        register_match_result(self.match, score="6-3 6-2", winner=self.teams[0])
        updated = register_match_result(self.match, score="3-6 2-6")
        self.assertEqual(updated.winner_id, self.teams[1].pk)
        self.assertEqual(updated.score, "3-6 2-6")

    def test_panel_informs_score_accommodation(self):
        user = get_user_model().objects.create_user(
            "mesita", password="clave-segura-654", is_staff=True
        )
        self.client.force_login(user)
        response = self.client.post(
            f"/gestion/ganador-elegido/resultados/{self.match.pk}/registrar/",
            {"score": "6-3 6-2", "winner": self.teams[1].pk},
            follow=True,
        )
        self.assertContains(response, "acomodado")
        self.match.refresh_from_db()
        self.assertEqual(self.match.winner_id, self.teams[1].pk)
        self.assertEqual(self.match.score, "3-6 2-6")


def zone_match(zone):
    return zone.matches.filter(stage="group").get()


# ---------------------------------------------------------------------------
# Disponibilidad de jugadores y garantías de base de datos
# ---------------------------------------------------------------------------


class PlayerAvailabilityTests(TournamentFixtureMixin, TestCase):
    def setUp(self):
        self.tournament = self.make_tournament(slug="disponibles")
        self.players = [Player.objects.create(first_name=f"D{i}", last_name="Uno", gender="M") for i in range(4)]

    def test_player_cannot_join_two_teams_in_same_tournament(self):
        players_service.create_team(self.tournament, self.players[0], self.players[1])
        with self.assertRaises(ValidationError):
            players_service.create_team(self.tournament, self.players[0], self.players[2])

    def test_player_can_join_teams_in_different_tournaments(self):
        other = self.make_tournament(slug="otro-torneo")
        players_service.create_team(self.tournament, self.players[0], self.players[1])
        players_service.create_team(other, self.players[0], self.players[1])
        self.assertEqual(TournamentPlayer.objects.filter(player=self.players[0]).count(), 2)

    def test_available_players_excludes_registered(self):
        players_service.create_team(self.tournament, self.players[0], self.players[1])
        available = set(players_service.available_players(self.tournament).values_list("id", flat=True))
        self.assertNotIn(self.players[0].pk, available)
        self.assertNotIn(self.players[1].pk, available)
        self.assertIn(self.players[2].pk, available)

    def test_database_unique_constraint_blocks_duplicate_entry(self):
        team_a = players_service.create_team(self.tournament, self.players[0], self.players[1])
        team_b = Team.objects.create(player1=self.players[0], player2=self.players[2])
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TournamentPlayer.objects.create(
                    tournament=self.tournament, player=self.players[0], team=team_b
                )

    def test_releasing_team_frees_players(self):
        team = players_service.create_team(self.tournament, self.players[0], self.players[1])
        players_service.release_team(self.tournament, team)
        available = set(players_service.available_players(self.tournament).values_list("id", flat=True))
        self.assertIn(self.players[0].pk, available)
        # y pueden formar otra dupla dentro del mismo torneo
        players_service.create_team(self.tournament, self.players[0], self.players[2])

    def test_new_team_gets_default_name_from_surnames(self):
        ana = Player.objects.create(first_name="Ana", last_name="Gómez", gender="F")
        bea = Player.objects.create(first_name="Bea", last_name="Ferreyra", gender="F")
        team = players_service.create_team(self.tournament, ana, bea)
        self.assertEqual(team.name, "Gómez - Ferreyra")

    def test_explicit_team_name_is_respected(self):
        ana = Player.objects.create(first_name="Ana", last_name="Gómez", gender="F")
        bea = Player.objects.create(first_name="Bea", last_name="Ferreyra", gender="F")
        team = players_service.create_team(self.tournament, ana, bea, name="Las Cóndoras")
        self.assertEqual(team.name, "Las Cóndoras")

    def test_reused_team_without_name_takes_default(self):
        ana = Player.objects.create(first_name="Ana", last_name="Gómez", gender="F")
        bea = Player.objects.create(first_name="Bea", last_name="Ferreyra", gender="F")
        team = Team.objects.create(player1=ana, player2=bea, name="")
        reused = players_service.create_team(self.tournament, ana, bea)
        reused.refresh_from_db()
        self.assertEqual(reused.pk, team.pk)
        self.assertEqual(reused.name, "Gómez - Ferreyra")

    def test_team_display_name_uses_name_or_surnames(self):
        ana = Player.objects.create(first_name="Ana", last_name="Gómez", gender="F")
        bea = Player.objects.create(first_name="Bea", last_name="Ferreyra", gender="F")
        team = Team.objects.create(player1=ana, player2=bea, name="")
        self.assertEqual(team.display_name, "Gómez / Ferreyra")
        team.name = "Las Cóndoras"
        self.assertEqual(team.display_name, "Las Cóndoras")

    def test_delete_player_removes_orphan_teams(self):
        ana = Player.objects.create(first_name="Ana", last_name="Gómez", gender="F")
        bea = Player.objects.create(first_name="Bea", last_name="Ferreyra", gender="F")
        team = Team.objects.create(player1=ana, player2=bea, name="")
        stats = players_service.delete_player(ana)
        self.assertEqual(stats["teams"], 1)
        self.assertFalse(Player.objects.filter(pk=ana.pk).exists())
        self.assertFalse(Team.objects.filter(pk=team.pk).exists())
        self.assertTrue(Player.objects.filter(pk=bea.pk).exists())

    def test_delete_player_blocked_when_registered(self):
        ana = Player.objects.create(first_name="Ana", last_name="Gómez", gender="F")
        bea = Player.objects.create(first_name="Bea", last_name="Ferreyra", gender="F")
        players_service.create_team(self.tournament, ana, bea)
        with self.assertRaisesMessage(ValidationError, "está en el torneo"):
            players_service.delete_player(ana)
        self.assertTrue(Player.objects.filter(pk=ana.pk).exists())

    def test_delete_player_blocked_when_team_has_matches(self):
        ana = Player.objects.create(first_name="Ana", last_name="Gómez", gender="F")
        bea = Player.objects.create(first_name="Bea", last_name="Ferreyra", gender="F")
        rival1 = Player.objects.create(first_name="Riva", last_name="Uno", gender="F")
        rival2 = Player.objects.create(first_name="Riva", last_name="Dos", gender="F")
        team = Team.objects.create(player1=ana, player2=bea, name="")
        other = Team.objects.create(player1=rival1, player2=rival2, name="")
        Match.objects.create(tournament=self.tournament, stage="group", round_number=1, slot=1, team1=team, team2=other)
        with self.assertRaisesMessage(ValidationError, "tiene partidos en"):
            players_service.delete_player(ana)
        self.assertTrue(Player.objects.filter(pk=ana.pk).exists())

    def test_registration_and_zone_conflicts_raise(self):
        team_a = players_service.create_team(self.tournament, self.players[0], self.players[1])
        duplicate_team = Team.objects.create(player1=self.players[0], player2=self.players[3])
        with self.assertRaises(ValidationError):
            with transaction.atomic():
                Registration.objects.create(tournament=self.tournament, team=duplicate_team, status="confirmed")


# ---------------------------------------------------------------------------
# Zonas
# ---------------------------------------------------------------------------


class ZoneTests(TournamentFixtureMixin, TestCase):
    def setUp(self):
        self.tournament = self.make_tournament(slug="zonas")
        self.teams = self.add_teams(self.tournament, 4)

    def test_team_must_be_registered_to_join_zone(self):
        outsider = Team.objects.create(
            player1=Player.objects.create(first_name="X", last_name="Y", gender="M"),
            player2=Player.objects.create(first_name="Z", last_name="W", gender="M"),
        )
        zone = Zone.objects.create(tournament=self.tournament, name="A", order=1)
        with self.assertRaises(ValidationError):
            players_service.assign_team_to_zone(zone, outsider)

    def test_team_belongs_to_single_zone(self):
        zones = self.make_zones(self.tournament, [2, 2])
        with self.assertRaises(ValidationError):
            players_service.assign_team_to_zone(zones[0], self.teams[2])

    def test_generate_zones_is_audited_and_creates_round_robin(self):
        zones = players_service.generate_zones(self.tournament)
        self.assertTrue(zones)
        assigned = ZoneTeam.objects.filter(zone__tournament=self.tournament).count()
        self.assertEqual(assigned, 4)
        total_matches = Match.objects.filter(tournament=self.tournament, stage="group").count()
        self.assertEqual(total_matches, 2)  # dos zonas de dos duplas
        audit = TieBreakAudit.objects.get(tournament=self.tournament, scope_type="zone_draw")
        self.assertTrue(audit.tiebreaker_seed)
        self.assertEqual(sorted(audit.result_ordered), sorted(audit.participants_ordered))

    def test_generate_zones_blocked_with_results(self):
        zones = self.make_zones(self.tournament, [2, 2])
        self.play_zone_group(zones[0])
        with self.assertRaises(ValidationError):
            players_service.generate_zones(self.tournament)

    def test_generate_zones_completes_existing_zones(self):
        self.make_zones(self.tournament, [2])
        zones = players_service.generate_zones(self.tournament)
        self.assertEqual(len(zones), 2)
        self.assertEqual(
            ZoneTeam.objects.filter(zone__tournament=self.tournament).count(), 4
        )
        self.assertEqual(
            Match.objects.filter(tournament=self.tournament, stage="group").count(), 2
        )

    def test_assign_team_creates_missing_matches(self):
        zone = Zone.objects.create(tournament=self.tournament, name="A", order=1)
        players_service.assign_team_to_zone(zone, self.teams[0])
        self.assertEqual(zone.matches.count(), 0)
        players_service.assign_team_to_zone(zone, self.teams[1])
        self.assertEqual(zone.matches.filter(stage="group").count(), 1)

    def test_zone_team_allow_players_once(self):
        self.make_zones(self.tournament, [2, 2])
        zone = Zone.objects.create(tournament=self.tournament, name="C", order=3)
        with self.assertRaises(ValidationError):
            ZoneTeam.objects.create(zone=zone, team=self.teams[0])


class ManualZonesTests(TournamentFixtureMixin, TestCase):
    """Armado manual de zonas: el organizador decide la composición."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("armador", password="clave-segura-321", is_staff=True)
        self.complex = Complex.objects.create(name="Complejo Manual", slug="complejo-manual")
        self.complex.users.add(self.user)
        self.tournament = self.make_tournament(
            slug="manual-zonas", complex_obj=self.complex, teams_per_zone=3
        )
        self.teams = self.add_teams(self.tournament, 4)
        self.client.force_login(self.user)

    def _post(self, destinations, **extra):
        payload = {
            f"team_{team.pk}": destinations[index] for index, team in enumerate(self.teams)
        }
        payload.update(extra)
        return self.client.post("/gestion/manual-zonas/zonas/manual/", payload, follow=True)

    def test_zones_page_offers_both_modes(self):
        response = self.client.get("/gestion/manual-zonas/zonas/")
        self.assertContains(response, "Generar zonas automáticamente")
        self.assertContains(response, "Armar zonas a mano")

    def test_manual_page_shows_current_zone_selected(self):
        zones = self.make_zones(self.tournament, [2, 2])
        response = self.client.get("/gestion/manual-zonas/zonas/manual/")
        self.assertContains(response, "Armar zonas a mano")
        self.assertContains(response, f'value="z:{zones[0].pk}" selected')
        self.assertContains(response, f'value="z:{zones[1].pk}" selected')
        self.assertContains(response, f'name="team_{self.teams[0].pk}"')

    def test_manual_assignment_creates_zones_and_round_robin(self):
        response = self._post(["new:1", "new:1", "new:2", "new:2"])
        self.assertContains(response, "quedaron como las armaste")
        zones = list(self.tournament.zones.order_by("order"))
        self.assertEqual([zone.name for zone in zones], ["A", "B"])
        self.assertEqual([zone.teams.count() for zone in zones], [2, 2])
        self.assertEqual(
            Match.objects.filter(tournament=self.tournament, stage="group").count(), 2
        )

    def test_manual_swap_rebuilds_matches(self):
        zone_a, zone_b = self.make_zones(self.tournament, [2, 2])
        self._post(
            [
                f"z:{zone_b.pk}",
                f"z:{zone_a.pk}",
                f"z:{zone_a.pk}",
                f"z:{zone_b.pk}",
            ]
        )
        pairs = {}
        for zone in (zone_a, zone_b):
            pairs[zone.pk] = {
                frozenset((match.team1_id, match.team2_id))
                for match in Match.objects.filter(
                    tournament=self.tournament, zone=zone, stage="group"
                )
            }
        self.assertEqual(pairs[zone_a.pk], {frozenset((self.teams[1].pk, self.teams[2].pk))})
        self.assertEqual(pairs[zone_b.pk], {frozenset((self.teams[0].pk, self.teams[3].pk))})

    def test_played_team_cannot_move(self):
        zone_a, zone_b = self.make_zones(self.tournament, [2, 2])
        self.play_zone_group(zone_a)
        response = self._post(
            [
                f"z:{zone_b.pk}",
                f"z:{zone_a.pk}",
                f"z:{zone_a.pk}",
                f"z:{zone_b.pk}",
            ]
        )
        self.assertContains(response, "ya tiene resultados cargados en su zona")
        self.assertTrue(ZoneTeam.objects.filter(zone=zone_a, team=self.teams[0]).exists())
        self.assertTrue(ZoneTeam.objects.filter(zone=zone_b, team=self.teams[3]).exists())

    def test_single_team_zone_is_rejected(self):
        response = self._post(["new:1", "", "new:2", "new:2"])
        self.assertContains(response, "una sola dupla")
        self.assertFalse(self.tournament.zones.exists())

    def test_zone_over_configured_size_is_rejected(self):
        response = self._post(["new:1", "new:1", "new:1", "new:1"])
        self.assertContains(response, "el máximo configurado es 3")
        self.assertFalse(self.tournament.zones.exists())

    def test_emptied_zone_is_removed(self):
        zone_a, zone_b = self.make_zones(self.tournament, [2, 2])
        self._post(["", "", f"z:{zone_b.pk}", f"z:{zone_b.pk}"])
        self.assertFalse(Zone.objects.filter(pk=zone_a.pk).exists())
        self.assertTrue(Zone.objects.filter(pk=zone_b.pk).exists())
        self.assertEqual(ZoneTeam.objects.filter(zone__tournament=self.tournament).count(), 2)

    def test_invalid_selection_is_rejected(self):
        response = self._post(["z:999999", "", "", ""])
        self.assertContains(response, "no es válida")

    def test_manual_zones_requires_access(self):
        self.client.logout()
        self.assertEqual(self.client.get("/gestion/manual-zonas/zonas/manual/").status_code, 302)
        outsider = get_user_model().objects.create_user("ajeno", password="clave-segura-999")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get("/gestion/manual-zonas/zonas/manual/").status_code, 403)


# ---------------------------------------------------------------------------
# Partidos de zona
# ---------------------------------------------------------------------------


class GroupMatchTests(TournamentFixtureMixin, TestCase):
    def setUp(self):
        self.tournament = self.make_tournament(slug="partidos")
        self.teams = self.add_teams(self.tournament, 3)
        self.zones = self.make_zones(self.tournament, [2])
        self.zone = self.zones[0]

    def test_create_group_match_validates_zone_membership(self):
        with self.assertRaises(ValidationError):
            create_group_match(self.tournament, self.zone, self.teams[0], self.teams[2])

    def test_duplicate_group_match_rejected_even_reversed(self):
        existing = self.zone.matches.get(stage="group")
        with self.assertRaises(ValidationError):
            create_group_match(self.tournament, self.zone, existing.team2, existing.team1)

    def test_database_constraint_blocks_duplicate_pair(self):
        existing = self.zone.matches.get(stage="group")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Match.objects.create(
                    tournament=self.tournament,
                    stage="group",
                    zone=self.zone,
                    slot=99,
                    team1=existing.team2,
                    team2=existing.team1,
                )

    def test_delete_group_match_only_without_result(self):
        match = self.zone.matches.get(stage="group")
        delete_group_match(match)
        self.assertFalse(Match.objects.filter(pk=match.pk).exists())

    def test_delete_group_match_with_result_blocked(self):
        match = self.zone.matches.get(stage="group")
        self.play_match(match, match.team1)
        with self.assertRaises(ValidationError):
            delete_group_match(match)


# ---------------------------------------------------------------------------
# Clasificación, desempates y auditoría
# ---------------------------------------------------------------------------


class StandingsTests(TournamentFixtureMixin, TestCase):
    def test_provisional_vs_official(self):
        tournament = self.make_tournament(slug="tabla")
        self.add_teams(tournament, 3)
        zone = self.make_zones(tournament, [3])[0]
        partial = draws.basic_zone_standings(zone)
        self.assertFalse(partial[0]["official"] if partial else False)
        self.play_zone_group(zone)
        official = draws.official_zone_standings(zone)
        self.assertTrue(all(row["official"] for row in official))
        self.assertEqual(len(official), 3)

    def test_official_standings_require_complete_zone(self):
        tournament = self.make_tournament(slug="incompleta")
        self.add_teams(tournament, 3)
        zone = self.make_zones(tournament, [3])[0]
        with self.assertRaises(ValueError):
            draws.official_zone_standings(zone)

    def test_points_then_set_diff_then_game_diff(self):
        tournament = self.make_tournament(slug="criterios")
        self.add_teams(tournament, 3)
        zone = self.make_zones(tournament, [3])[0]
        matches = {tuple(sorted((m.team1_id, m.team2_id))): m for m in zone.matches.all()}
        teams = list(zone.teams.all())
        a, b, c = teams
        # A vence a B 6-0 6-0
        self.play_match(matches[tuple(sorted((a.pk, b.pk)))], a, score="6-0 6-0")
        # B vence a C 6-0 6-0
        self.play_match(matches[tuple(sorted((b.pk, c.pk)))], b, score="6-0 6-0")
        # C vence a A 6-4 6-3 visto desde A -> 4-6 3-6; todos 3 puntos, dif. sets 0,
        # dif. games: A +7, B 0, C -7
        self.play_match(matches[tuple(sorted((a.pk, c.pk)))], c, score="4-6 3-6")
        standings = draws.official_zone_standings(zone)
        order = [row["team"].pk for row in standings]
        self.assertEqual(order, [a.pk, b.pk, c.pk])

    def test_equal_teams_resolved_with_auditable_reproducible_draw(self):
        tournament = self.make_tournament(slug="sorteo")
        self.add_teams(tournament, 3)
        zone = self.make_zones(tournament, [3])[0]
        matches = {tuple(sorted((m.team1_id, m.team2_id))): m for m in zone.matches.all()}
        a, b, c = list(zone.teams.all())
        self.play_match(matches[tuple(sorted((a.pk, b.pk)))], a, score="6-0 6-0")
        self.play_match(matches[tuple(sorted((b.pk, c.pk)))], b, score="6-0 6-0")
        self.play_match(matches[tuple(sorted((a.pk, c.pk)))], c, score="0-6 0-6")
        first = draws.official_zone_standings(zone)
        audits = TieBreakAudit.objects.filter(tournament=tournament, scope_type="zone_standings")
        self.assertEqual(audits.count(), 1)
        # Recalcular no genera un segundo sorteo ni cambia el orden.
        second = draws.official_zone_standings(zone)
        self.assertEqual([r["team"].pk for r in first], [r["team"].pk for r in second])
        self.assertEqual(
            TieBreakAudit.objects.filter(tournament=tournament, scope_type="zone_standings").count(), 1
        )
        audit = audits.get()
        self.assertEqual(sorted(audit.participants_ordered), sorted([a.pk, b.pk, c.pk]))

    def test_tiebreak_audit_is_append_only(self):
        tournament = self.make_tournament(slug="auditoria")
        audit = TieBreakAudit.objects.create(
            tournament=tournament,
            scope_type="zone_standings",
            scope_key="x",
            input_signature="sig",
            tiebreaker_seed=1,
            participants_ordered=[1, 2],
            result_ordered=[2, 1],
        )
        with self.assertRaises(ValidationError):
            audit.save()
        with self.assertRaises(ValidationError):
            audit.delete()

    def test_zone_standings_use_no_head_to_head(self):
        """Con empate total y sin sorteo previo, el orden es determinista y auditado,
        no basado en el resultado entre ellos."""
        tournament = self.make_tournament(slug="sin-h2h")
        self.add_teams(tournament, 2)
        zone = self.make_zones(tournament, [2])[0]
        match = zone.matches.get(stage="group")
        self.play_match(match, match.team1, score="6-0 6-0")
        standings = draws.official_zone_standings(zone)
        self.assertEqual(standings[0]["team"].pk, match.team1_id)
        self.assertEqual(standings[1]["team"].pk, match.team2_id)


class CrossZoneQualificationTests(TournamentFixtureMixin, TestCase):
    def test_first_positions_rank_before_seconds(self):
        tournament = self.make_tournament(slug="cross")
        self.add_teams(tournament, 4)
        zones = self.make_zones(tournament, [2, 2])
        for zone in zones:
            match = zone.matches.get(stage="group")
            self.play_match(match, match.team1, score="6-0 6-0")
        detailed = draws.qualified_teams_detailed(tournament)
        positions = [item["zone_position"] for item in detailed]
        self.assertEqual(positions[:2], [1, 1])
        self.assertEqual(positions[2:], [2, 2])
        self.assertEqual([item["rank"] for item in detailed], [1, 2, 3, 4])

    def test_equal_cross_zone_records_resolved_with_audit(self):
        tournament = self.make_tournament(slug="cross-draw")
        self.add_teams(tournament, 4)
        zones = self.make_zones(tournament, [2, 2])
        for zone in zones:
            match = zone.matches.get(stage="group")
            self.play_match(match, match.team1, score="6-0 6-0")
        draws.qualified_teams_detailed(tournament)
        self.assertTrue(
            TieBreakAudit.objects.filter(tournament=tournament, scope_type="cross_zone_position").exists()
        )


# ---------------------------------------------------------------------------
# Llaves: tamaños con descansos y progresión
# ---------------------------------------------------------------------------


class BracketByesTests(TournamentFixtureMixin, TestCase):
    def _assert_bracket(self, teams_count):
        tournament = self.completed_tournament(teams_count, slug=f"llave-{teams_count}")
        detailed = draws.qualified_teams_detailed(tournament)
        n = len(detailed)
        self.assertEqual(n, teams_count)
        p = 1
        while p < n:
            p *= 2
        byes = p - n
        matches = list(
            Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES).order_by("round_number", "slot")
        )
        self.assertEqual(len(matches), p - 1, f"Árbol completo para {n} clasificados")
        depths = sorted({m.round_number for m in matches})
        self.assertEqual(len(depths), p.bit_length() - 1)
        first = [m for m in matches if m.round_number == 1]
        self.assertEqual(len(first), p // 2)
        stage_for_field = {2: "final", 4: "semi", 8: "quarter", 16: "round_of_16", 32: "round_of_32"}
        self.assertIn(p, stage_for_field, f"llave de {p} sin etapa definida")
        self.assertEqual(
            first[0].stage,
            stage_for_field[p],
            f"la primera ronda de una llave de {p} usa la etapa correcta",
        )
        bye_rows = [m for m in first if m.is_bye]
        self.assertEqual(len(bye_rows), byes)
        self.assertEqual(
            {m.winner_id for m in bye_rows},
            {item["team"].pk for item in detailed[:byes]},
            "los descansos van a las mejores clasificadas",
        )
        for row in bye_rows:
            present = [team_id for team_id in (row.team1_id, row.team2_id) if team_id]
            self.assertEqual(len(present), 1, "un descanso tiene exactamente una dupla real")
            self.assertEqual(row.winner_id, present[0])
            self.assertEqual(row.score, "")
        for row in matches:
            if row.round_number < max(depths):
                self.assertIsNotNone(row.next_match_id, "todo cruce alimenta un partido siguiente")
                self.assertIn(row.next_slot, (1, 2))
        # cada partido de rondas siguientes recibe exactamente dos feeders distintos
        for row in matches:
            if row.round_number > 1:
                slots = [
                    (feeder.next_slot) for feeder in Match.objects.filter(next_match=row)
                ]
                self.assertEqual(sorted(slots), [1, 2])
        finals = [m for m in matches if m.stage == "final"]
        self.assertEqual(len(finals), 1, "una sola final y sin tercer puesto")
        # jugar hasta el campeón
        final = self.play_playoffs(tournament)
        final.refresh_from_db()
        self.assertIsNotNone(final.winner_id)
        tournament.refresh_from_db()
        self.assertEqual(tournament.status, "finished")
        return tournament

    def test_bracket_size_5(self):
        self._assert_bracket(5)

    def test_bracket_size_6(self):
        self._assert_bracket(6)

    def test_bracket_size_7(self):
        self._assert_bracket(7)

    def test_bracket_size_9(self):
        self._assert_bracket(9)

    def test_bracket_size_10(self):
        self._assert_bracket(10)

    def test_bracket_size_11(self):
        self._assert_bracket(11)

    def test_bracket_size_2(self):
        self._assert_bracket(2)

    def test_bracket_size_3(self):
        self._assert_bracket(3)

    def test_bracket_size_4(self):
        self._assert_bracket(4)

    def test_bracket_size_8(self):
        self._assert_bracket(8)

    def test_bracket_size_12(self):
        self._assert_bracket(12)

    def test_bracket_size_16(self):
        """Octavos completos (16 clasificados, sin descansos)."""
        self._assert_bracket(16)

    def test_bracket_size_17(self):
        """Dieciseisavos con descansos (17 clasificados)."""
        self._assert_bracket(17)

    def test_bracket_size_24(self):
        """Dieciseisavos con 8 descansos (24 clasificados)."""
        self._assert_bracket(24)

    def test_bracket_size_32(self):
        """Dieciseisavos completos (32 clasificados, sin descansos)."""
        self._assert_bracket(32)

    def test_first_round_avoids_same_zone_when_possible(self):
        tournament = self.make_tournament(slug="cruces")
        self.add_teams(tournament, 6)
        zones = self.make_zones(tournament, [3, 3])
        for zone in zones:
            matches = list(zone.matches.filter(stage="group").order_by("slot"))
            self.play_match(matches[0], matches[0].team1, score="6-0 6-0")
            self.play_match(matches[1], matches[1].team1, score="6-0 6-0")
            self.play_match(matches[2], matches[2].team1, score="6-0 6-0")
        first = list(
            Match.objects.filter(tournament=tournament, round_number=1, stage__in=PLAYOFF_STAGES).select_related("team1", "team2")
        )
        self.assertEqual(len(first), 2, "4 clasificados: dos cruces")
        for match in first:
            zone1 = ZoneTeam.objects.get(team=match.team1).zone_id
            zone2 = ZoneTeam.objects.get(team=match.team2).zone_id
            self.assertNotEqual(zone1, zone2, "debe evitarse el cruce de misma zona")

    def test_generation_is_idempotent(self):
        tournament = self.completed_tournament(6, slug="idempotente")
        first_count = Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES).count()
        draws.ensure_playoff_bracket(tournament)
        draws.ensure_playoff_bracket(tournament)
        self.assertEqual(
            Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES).count(), first_count
        )

    def test_generation_blocked_with_played_bracket(self):
        tournament = self.completed_tournament(4, slug="bloqueo")
        semi = Match.objects.filter(tournament=tournament, stage="semi").first()
        self.play_match(semi, semi.team1)
        with self.assertRaises(BracketConflictError):
            draws.generate_playoff_bracket(tournament)

    def test_bracket_not_generated_without_enough_qualifiers(self):
        tournament = self.make_tournament(slug="sin-llave", qualifiers_per_zone=1)
        self.add_teams(tournament, 2)
        zone = self.make_zones(tournament, [2])[0]
        match = zone.matches.get(stage="group")
        self.play_match(match, match.team1)
        self.assertFalse(Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES).exists())

    def test_round_robin_format_does_not_generate_bracket(self):
        tournament = self.make_tournament(slug="round-robin", format_type="round_robin")
        self.add_teams(tournament, 4)
        zones = self.make_zones(tournament, [2, 2])
        for zone in zones:
            match = zone.matches.get(stage="group")
            self.play_match(match, match.team1)
        self.assertEqual(draws.ensure_playoff_bracket(tournament), [])
        self.assertFalse(Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES).exists())


class ManualBracketTests(TournamentFixtureMixin, TestCase):
    """Armado manual de los cruces de primera ronda."""

    def _info_and_teams(self, teams_count=6, slug="manual"):
        tournament = self.completed_tournament(teams_count, slug=slug)
        info = draws.playoff_bracket_info(tournament)
        return tournament, info, [entry["team"] for entry in info["detailed"]]

    def test_plan_lists_classified_and_byes(self):
        tournament, info, teams = self._info_and_teams()
        self.assertEqual(info["n"], 6)
        self.assertEqual(info["p"], 8)
        self.assertEqual(info["matches"], 4)
        self.assertEqual(info["byes"], 2)
        self.assertEqual(len(teams), 6)
        self.assertEqual(len({team.pk for team in teams}), 6)

    def test_generation_respects_chosen_crossings(self):
        tournament, info, teams = self._info_and_teams()
        selections = [
            (teams[0].pk, teams[1].pk),
            (teams[2].pk, teams[3].pk),
            (teams[4].pk, None),
            (None, teams[5].pk),
        ]
        draws.generate_playoff_bracket_manual(tournament, selections)
        tournament.refresh_from_db()
        self.assertTrue(tournament.manual_bracket)
        rows = list(draws.first_round_matches(tournament))
        self.assertEqual(len(rows), 4)
        self.assertEqual((rows[0].team1_id, rows[0].team2_id), (teams[0].pk, teams[1].pk))
        self.assertEqual((rows[1].team1_id, rows[1].team2_id), (teams[2].pk, teams[3].pk))
        self.assertEqual((rows[2].team1_id, rows[2].team2_id), (teams[4].pk, None))
        self.assertEqual((rows[3].team1_id, rows[3].team2_id), (None, teams[5].pk))
        self.assertTrue(rows[2].is_bye)
        self.assertEqual(rows[2].winner_id, teams[4].pk)
        self.assertFalse(rows[0].is_bye)
        self.assertIsNone(rows[0].winner_id)
        self.assertEqual(
            Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES).count(), 7
        )

    def test_generation_allows_same_zone_crossing(self):
        tournament, info, teams = self._info_and_teams(slug="manual-misma-zona")
        # Dos duplas de la misma zona enfrentadas a mano (la siembra automática lo evita).
        by_zone: dict[int, list] = {}
        for entry in info["detailed"]:
            by_zone.setdefault(entry["zone"].pk, []).append(entry["team"])
        same_zone = next(group for group in by_zone.values() if len(group) == 2)
        used = {same_zone[0].pk, same_zone[1].pk}
        remaining = [team for team in teams if team.pk not in used]
        selections = [
            (same_zone[0].pk, same_zone[1].pk),
            (remaining[0].pk, remaining[1].pk),
            (remaining[2].pk, None),
            (None, remaining[3].pk),
        ]
        draws.generate_playoff_bracket_manual(tournament, selections)
        rows = list(draws.first_round_matches(tournament))
        self.assertEqual((rows[0].team1_id, rows[0].team2_id), (same_zone[0].pk, same_zone[1].pk))

    def test_generation_rejects_empty_crossing(self):
        tournament, info, teams = self._info_and_teams(slug="manual-vacio")
        selections = [
            (teams[0].pk, teams[1].pk),
            (teams[2].pk, teams[3].pk),
            (teams[4].pk, None),
            (None, None),
        ]
        with self.assertRaises(BracketError):
            draws.generate_playoff_bracket_manual(tournament, selections)

    def test_generation_rejects_duplicate_team(self):
        tournament, info, teams = self._info_and_teams(slug="manual-repetida")
        selections = [
            (teams[0].pk, teams[1].pk),
            (teams[0].pk, teams[3].pk),
            (teams[4].pk, None),
            (None, teams[5].pk),
        ]
        with self.assertRaises(BracketError):
            draws.generate_playoff_bracket_manual(tournament, selections)

    def test_generation_rejects_unclassified_team(self):
        tournament, info, teams = self._info_and_teams(slug="manual-ajena")
        other = self.make_tournament(slug="otro-manual")
        stranger = self.add_team(other)
        selections = [
            (teams[0].pk, stranger.pk),
            (teams[2].pk, teams[3].pk),
            (teams[4].pk, None),
            (None, teams[5].pk),
        ]
        with self.assertRaises(BracketError):
            draws.generate_playoff_bracket_manual(tournament, selections)

    def test_generation_rejects_missing_teams(self):
        tournament, info, teams = self._info_and_teams(slug="manual-faltan")
        selections = [
            (teams[0].pk, teams[1].pk),
            (teams[2].pk, teams[3].pk),
            (teams[4].pk, None),
            (None, None),
        ]
        with self.assertRaises(BracketError):
            draws.generate_playoff_bracket_manual(tournament, selections)

    def test_ensure_keeps_manual_crossings(self):
        tournament, info, teams = self._info_and_teams(slug="manual-asegura")
        selections = [
            (teams[1].pk, teams[0].pk),
            (teams[3].pk, teams[2].pk),
            (None, teams[4].pk),
            (teams[5].pk, None),
        ]
        draws.generate_playoff_bracket_manual(tournament, selections)
        before = Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES).count()
        draws.ensure_playoff_bracket(tournament)
        tournament.refresh_from_db()
        self.assertTrue(tournament.manual_bracket)
        self.assertEqual(
            Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES).count(), before
        )
        rows = list(draws.first_round_matches(tournament))
        self.assertEqual((rows[0].team1_id, rows[0].team2_id), (teams[1].pk, teams[0].pk))
        self.assertEqual((rows[2].team1_id, rows[2].team2_id), (None, teams[4].pk))

    def test_auto_generation_replaces_manual_arrangement(self):
        tournament, info, teams = self._info_and_teams(slug="manual-auto")
        selections = [
            (teams[1].pk, teams[0].pk),
            (teams[3].pk, teams[2].pk),
            (None, teams[4].pk),
            (teams[5].pk, None),
        ]
        draws.generate_playoff_bracket_manual(tournament, selections)
        self.assertTrue(tournament.manual_bracket)
        draws.generate_playoff_bracket(tournament)
        tournament.refresh_from_db()
        self.assertFalse(tournament.manual_bracket)
        rows = list(draws.first_round_matches(tournament))
        self.assertEqual(len(rows), 4)
        placed = {team_id for row in rows for team_id in (row.team1_id, row.team2_id) if team_id}
        self.assertEqual(placed, {team.pk for team in teams})


class AutomaticProgressionTests(TournamentFixtureMixin, TestCase):
    def test_last_zone_result_generates_bracket_automatically(self):
        tournament = self.make_tournament(slug="auto")
        self.add_teams(tournament, 4)
        zones = self.make_zones(tournament, [2, 2])
        first = zones[0].matches.get(stage="group")
        second = zones[1].matches.get(stage="group")
        self.play_match(first, first.team1)
        self.assertFalse(Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES).exists())
        self.play_match(second, second.team1)
        self.assertTrue(Match.objects.filter(tournament=tournament, stage__in=PLAYOFF_STAGES).exists())

    def test_winner_advances_to_correct_next_match(self):
        tournament = self.completed_tournament(4, slug="avance")
        semis = list(Match.objects.filter(tournament=tournament, stage="semi").order_by("slot"))
        final = Match.objects.get(tournament=tournament, stage="final")
        self.play_match(semis[0], semis[0].team2)
        final.refresh_from_db()
        self.assertEqual(final.team1_id, semis[0].team2_id)
        self.play_match(semis[1], semis[1].team1)
        final.refresh_from_db()
        self.assertEqual(final.team2_id, semis[1].team1_id)
        self.assertIsNone(final.winner_id)

    def test_final_winner_finishes_tournament(self):
        tournament = self.completed_tournament(4, slug="campeon")
        final = self.play_playoffs(tournament)
        final.refresh_from_db()
        tournament.refresh_from_db()
        self.assertIsNotNone(final.winner_id)
        self.assertEqual(tournament.status, "finished")

    def test_group_correction_allowed_while_bracket_unplayed(self):
        tournament = self.completed_tournament(4, slug="correccion-libre")
        match = Match.objects.filter(tournament=tournament, stage="group").first()
        register_match_result(match, score="0-6 0-6", winner=match.team2)
        match.refresh_from_db()
        self.assertEqual(match.winner_id, match.team2_id)

    def test_group_correction_blocked_when_bracket_has_results(self):
        tournament = self.completed_tournament(4, slug="correccion-bloqueada")
        semi = Match.objects.filter(tournament=tournament, stage="semi").first()
        self.play_match(semi, semi.team1)
        group = Match.objects.filter(tournament=tournament, stage="group").first()
        with self.assertRaises(ValidationError):
            register_match_result(group, score="0-6 0-6", winner=group.team2)

    def test_playoff_correction_blocked_with_dependent_result(self):
        tournament = self.completed_tournament(4, slug="dependencias")
        semis = list(Match.objects.filter(tournament=tournament, stage="semi").order_by("slot"))
        self.play_match(semis[0], semis[0].team1)
        self.play_match(semis[1], semis[1].team1)
        final = Match.objects.get(tournament=tournament, stage="final")
        self.play_match(final, final.team1)
        with self.assertRaises(ValidationError):
            register_match_result(semis[0], score="0-6 0-6", winner=semis[0].team2)

    def test_playoff_correction_allowed_when_dependent_empty(self):
        tournament = self.completed_tournament(4, slug="correccion-playoff")
        semi = Match.objects.filter(tournament=tournament, stage="semi").first()
        self.play_match(semi, semi.team1)
        register_match_result(semi, score="1-6 1-6", winner=semi.team2)
        semi.refresh_from_db()
        self.assertEqual(semi.winner_id, semi.team2_id)
        final = Match.objects.get(tournament=tournament, stage="final")
        self.assertEqual(final.team1_id, semi.team2_id)


# ---------------------------------------------------------------------------
# Multi-complejo y control de acceso
# ---------------------------------------------------------------------------


class AccessControlTests(TournamentFixtureMixin, TestCase):
    def setUp(self):
        User = get_user_model()
        self.user_a = User.objects.create_user("user_a", password="clave-segura-123", is_staff=True)
        self.user_b = User.objects.create_user("user_b", password="clave-segura-123", is_staff=True)
        self.staff = User.objects.create_user("staff", password="clave-segura-123", is_staff=True)
        self.superuser = User.objects.create_superuser("root", password="clave-segura-123")
        self.complex_a = Complex.objects.create(name="Complejo A", slug="complejo-a")
        self.complex_b = Complex.objects.create(name="Complejo B", slug="complejo-b")
        self.complex_a.users.add(self.user_a)
        self.complex_b.users.add(self.user_b)
        self.tournament_a = self.make_tournament(slug="torneo-a", complex_obj=self.complex_a)
        self.tournament_b = self.make_tournament(slug="torneo-b", complex_obj=self.complex_b)
        self.legacy = self.make_tournament(slug="torneo-heredado", complex_obj=None)

    def test_selection_lists_only_authorized_tournaments(self):
        self.client.force_login(self.user_a)
        response = self.client.get("/gestion/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Torneo torneo-a")
        self.assertNotContains(response, "Torneo torneo-b")

    def test_cannot_open_other_complex_panel(self):
        self.client.force_login(self.user_a)
        response = self.client.get("/gestion/torneo-b/")
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/gestion/torneo-b/parejas/crear/", {"player1": 1, "player2": 2})
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/gestion/torneo-b/jugadores/1/eliminar/")
        self.assertEqual(response.status_code, 403)

    def test_session_tampering_does_not_grant_access(self):
        self.client.force_login(self.user_a)
        session = self.client.session
        session["padel_tournament_id"] = self.tournament_b.pk
        session.save()
        response = self.client.get("/gestion/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Torneo torneo-b")
        self.assertEqual(self.client.get("/gestion/torneo-b/llaves/").status_code, 403)

    def test_zone_api_respects_complex(self):
        self.client.force_login(self.user_a)
        teams = self.add_teams(self.tournament_b, 2)
        zone = Zone.objects.create(tournament=self.tournament_b, name="A", order=1)
        ZoneTeam.objects.create(zone=zone, team=teams[0])
        ZoneTeam.objects.create(zone=zone, team=teams[1])
        response = self.client.get(f"/gestion/api/zonas/{zone.pk}/equipos/")
        self.assertEqual(response.status_code, 403)

    def test_legacy_tournament_available_to_staff(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/gestion/torneo-heredado/").status_code, 200)

    def test_superuser_sees_everything(self):
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get("/gestion/torneo-a/").status_code, 200)
        self.assertEqual(self.client.get("/gestion/torneo-b/").status_code, 200)

    def test_manual_bracket_respects_complex(self):
        self.client.force_login(self.user_a)
        self.assertEqual(self.client.get("/gestion/torneo-b/llaves/manual/").status_code, 403)
        self.assertEqual(self.client.post("/gestion/torneo-b/llaves/manual/", {}).status_code, 403)


# ---------------------------------------------------------------------------
# Flujo del panel por HTTP
# ---------------------------------------------------------------------------


class PanelFlowTests(TournamentFixtureMixin, TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("manager", password="clave-segura-123", is_staff=True)
        self.complex = Complex.objects.create(name="Complejo Panel", slug="complejo-panel")
        self.complex.users.add(self.user)
        self.tournament = self.make_tournament(slug="panel", complex_obj=self.complex)
        self.client.force_login(self.user)

    def test_panel_sections_render(self):
        for path in [
            "/gestion/",
            "/gestion/panel/",
            "/gestion/panel/parejas/",
            "/gestion/panel/jugadores/",
            "/gestion/panel/zonas/",
            "/gestion/panel/partidos/",
            "/gestion/panel/resultados/",
            "/gestion/panel/clasificacion/",
            "/gestion/panel/llaves/",
            "/gestion/panel/configuracion/",
            "/torneos/",
            "/torneos/panel/",
        ]:
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_home_button_appears_in_every_panel_menu_except_select(self):
        """El botón «Inicio» está en todos los menús del panel, menos en «Tus torneos»."""
        for path in [
            "/gestion/panel/",
            "/gestion/panel/parejas/",
            "/gestion/panel/jugadores/",
            "/gestion/panel/zonas/",
            "/gestion/panel/partidos/",
            "/gestion/panel/resultados/",
            "/gestion/panel/clasificacion/",
            "/gestion/panel/llaves/",
            "/gestion/panel/configuracion/",
        ]:
            response = self.client.get(path)
            self.assertContains(response, 'href="/gestion/salir/">Inicio</a>', msg_prefix=path)
            self.assertNotContains(response, "Cambiar torneo", msg_prefix=path)
        tournament_page = self.client.get("/gestion/panel/")
        self.assertContains(tournament_page, "panel-collapse--actions")
        self.assertContains(tournament_page, "panel-collapse--sections")
        settings_page = self.client.get("/gestion/configuracion/")
        self.assertContains(settings_page, 'href="/gestion/">Inicio</a>')
        self.assertContains(settings_page, "panel-collapse--actions")
        self.assertNotContains(settings_page, "panel-collapse--sections")
        self.assertNotContains(settings_page, "Volver al listado")
        select_page = self.client.get("/gestion/")
        self.assertNotContains(select_page, ">Inicio</a>")
        self.assertNotContains(select_page, "panel-collapse")
        self.assertNotContains(select_page, "Volver al listado")

    def test_pairs_form_exposes_team_name_default_hook(self):
        response = self.client.get("/gestion/panel/parejas/")
        self.assertContains(response, 'id="id_team_name"')
        self.assertContains(response, "pairs.js")

    def test_panel_team_create_without_name_uses_surnames(self):
        ana = Player.objects.create(first_name="Ana", last_name="Gómez", gender="F")
        bea = Player.objects.create(first_name="Bea", last_name="Ferreyra", gender="F")
        response = self.client.post(
            "/gestion/panel/parejas/crear/",
            {"player1": ana.pk, "player2": bea.pk, "name": ""},
            follow=True,
        )
        self.assertContains(response, "Pareja agregada")
        team = players_service.tournament_teams(self.tournament).get()
        self.assertEqual(team.name, "Gómez - Ferreyra")

    def test_panel_player_delete_removes_sheet_and_teams(self):
        player = Player.objects.create(first_name="Tete", last_name="Prueba", gender="M")
        partner = Player.objects.create(first_name="Pare", last_name="Ja", gender="M")
        team = Team.objects.create(player1=player, player2=partner, name="")
        response = self.client.post(f"/gestion/panel/jugadores/{player.pk}/eliminar/", follow=True)
        self.assertContains(response, "se eliminó de la base de jugadores")
        self.assertFalse(Player.objects.filter(pk=player.pk).exists())
        self.assertFalse(Team.objects.filter(pk=team.pk).exists())

    def test_panel_player_delete_blocked_keeps_sheet(self):
        player = Player.objects.create(first_name="Juga", last_name="Torneo", gender="M")
        partner = Player.objects.create(first_name="Compa", last_name="Ñera", gender="M")
        players_service.create_team(self.tournament, player, partner)
        response = self.client.post(f"/gestion/panel/jugadores/{player.pk}/eliminar/", follow=True)
        self.assertContains(response, "está en el torneo")
        self.assertTrue(Player.objects.filter(pk=player.pk).exists())

    def test_bracket_graphic_view_shows_duplas_and_connectors(self):
        for name in ("Alfa", "Beta", "Gamma", "Delta"):
            self.add_team(self.tournament, name=name)
        self.make_zones(self.tournament, [2, 2])
        for zone in self.tournament.zones.all():
            self.play_zone_group(zone)
        response = self.client.get("/gestion/panel/llaves/")
        self.assertContains(response, "Cuadro gráfico")
        self.assertContains(response, 'id="bracket-canvas"')
        self.assertContains(response, 'data-next="tree-match-')
        self.assertContains(response, "Clasificadas por zona")
        for name in ("Alfa", "Beta", "Gamma", "Delta"):
            self.assertContains(response, name)
        detalle = self.client.get("/gestion/panel/llaves/?vista=detalle")
        self.assertContains(detalle, "Lista de partidos")
        self.assertNotContains(detalle, 'id="bracket-canvas"')

    def test_bracket_graphic_shows_all_rounds_except_groups(self):
        """Con 8 clasificadas, el cuadro gráfico muestra Cuartos, Semifinal y Final."""
        self.add_teams(self.tournament, 8)
        self.make_zones(self.tournament, [2, 2, 2, 2])
        for zone in self.tournament.zones.all().order_by("order"):
            self.play_zone_group(zone)
        response = self.client.get("/gestion/panel/llaves/")
        content = response.content.decode()
        self.assertEqual(content.count('<div class="tree-round">'), 3)
        self.assertContains(response, "Cuartos")
        self.assertContains(response, "Semifinal")
        self.assertContains(response, "Final")
        self.assertContains(response, "Clasificadas por zona")
        self.assertNotContains(response, "Zona (round robin)")

    def test_bracket_graphic_renders_byes(self):
        """Cuadro gráfico con descansos: la dupla del bye se muestra sin romper la página."""
        self.add_teams(self.tournament, 6)
        self.make_zones(self.tournament, [2, 2, 2])
        for zone in self.tournament.zones.all().order_by("order"):
            self.play_zone_group(zone)
        response = self.client.get("/gestion/panel/llaves/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Descanso (bye)")
        self.assertContains(response, "tree-match--bye")

    def test_players_list_filters_by_category(self):
        Player.objects.create(first_name="Sexta", last_name="Alvarez", gender="F", level="sexta")
        Player.objects.create(first_name="Septima", last_name="Benitez", gender="F", level="septima")
        response = self.client.get("/gestion/panel/jugadores/?nivel=sexta")
        self.assertContains(response, "Alvarez")
        self.assertNotContains(response, "Benitez")
        response = self.client.get("/gestion/panel/jugadores/?nivel=todos")
        self.assertContains(response, "Alvarez")
        self.assertContains(response, "Benitez")

    def test_teams_page_suggests_players_by_category(self):
        Player.objects.create(first_name="Nivel", last_name="Sexta", gender="F", level="sexta")
        response = self.client.get("/gestion/panel/parejas/")
        self.assertContains(response, 'data-category="primera"')
        self.assertContains(response, 'id="show-all-levels"')
        self.assertContains(response, 'data-level="sexta"')
        self.assertContains(response, 'value="primera" selected')

    def test_manual_bracket_page_saves_chosen_crossings(self):
        self.add_teams(self.tournament, 4)
        self.make_zones(self.tournament, [2, 2])
        for zone in self.tournament.zones.all().order_by("order"):
            self.play_zone_group(zone)
        response = self.client.get("/gestion/panel/llaves/manual/")
        self.assertContains(response, "Armar cruces a mano")
        self.assertContains(response, "Clasificadas")
        teams = list(players_service.tournament_teams(self.tournament).order_by("pk"))
        payload = {
            "team1_1": teams[0].pk,
            "team2_1": teams[1].pk,
            "team1_2": teams[2].pk,
            "team2_2": teams[3].pk,
        }
        response = self.client.post("/gestion/panel/llaves/manual/", payload, follow=True)
        self.assertContains(response, "armada a tu manera")
        self.assertContains(response, "Cruces armados a mano")
        rows = list(draws.first_round_matches(self.tournament))
        self.assertEqual((rows[0].team1_id, rows[0].team2_id), (teams[0].pk, teams[1].pk))
        self.assertEqual((rows[1].team1_id, rows[1].team2_id), (teams[2].pk, teams[3].pk))
        self.assertFalse(rows[0].is_bye)

    def test_manual_bracket_rejects_incomplete_selection(self):
        self.add_teams(self.tournament, 4)
        self.make_zones(self.tournament, [2, 2])
        for zone in self.tournament.zones.all().order_by("order"):
            self.play_zone_group(zone)
        teams = list(players_service.tournament_teams(self.tournament).order_by("pk"))
        payload = {
            "team1_1": teams[0].pk,
            "team2_1": teams[1].pk,
            "team1_2": "",
            "team2_2": "",
        }
        response = self.client.post("/gestion/panel/llaves/manual/", payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "quedó vacío")

    def test_manual_bracket_blocked_when_bracket_has_results(self):
        self.add_teams(self.tournament, 4)
        self.make_zones(self.tournament, [2, 2])
        for zone in self.tournament.zones.all().order_by("order"):
            self.play_zone_group(zone)
        semi = Match.objects.filter(tournament=self.tournament, stage="semi").order_by("slot").first()
        self.play_match(semi, semi.team1)
        response = self.client.get("/gestion/panel/llaves/manual/", follow=True)
        self.assertContains(response, "ya tiene resultados cargados")

    def test_full_flow_creates_bracket_and_finishes(self):
        players = [Player.objects.create(first_name=f"F{i}", last_name="Flujo", gender="M") for i in range(8)]
        for i in range(4):
            response = self.client.post(
                "/gestion/panel/parejas/crear/",
                {"player1": players[2 * i].pk, "player2": players[2 * i + 1].pk, "name": ""},
                follow=True,
            )
            self.assertContains(response, "Pareja agregada")
        self.assertEqual(players_service.tournament_team_ids(self.tournament).__len__(), 4)

        response = self.client.post("/gestion/panel/zonas/generar/", follow=True)
        self.assertContains(response, "Zonas generadas")
        self.assertEqual(Match.objects.filter(tournament=self.tournament, stage="group").count(), 2)

        for match in Match.objects.filter(tournament=self.tournament, stage="group"):
            response = self.client.post(
                f"/gestion/panel/resultados/{match.pk}/registrar/",
                {"score": "6-4 6-3"},
                follow=True,
            )
            self.assertContains(response, "Resultado registrado")
        self.assertTrue(Match.objects.filter(tournament=self.tournament, stage__in=PLAYOFF_STAGES).exists())

        # Duplicar una pareja con un jugador ya usado falla y no crea datos.
        used = Player.objects.first()
        response = self.client.post(
            "/gestion/panel/parejas/crear/",
            {"player1": used.pk, "player2": players[1].pk, "name": ""},
            follow=True,
        )
        self.assertContains(response, "ya integran otra dupla")

    def test_invalid_score_shows_error_and_does_not_save(self):
        self.add_teams(self.tournament, 2)
        self.make_zones(self.tournament, [2])
        match = Match.objects.filter(tournament=self.tournament, stage="group").first()
        response = self.client.post(
            f"/gestion/panel/resultados/{match.pk}/registrar/",
            {"score": "6-5 6-3"},
            follow=True,
        )
        self.assertContains(response, "imposible")
        match.refresh_from_db()
        self.assertIsNone(match.winner_id)

    def test_walkover_via_panel(self):
        self.add_teams(self.tournament, 2)
        zone = self.make_zones(self.tournament, [2])[0]
        match = zone.matches.get(stage="group")
        response = self.client.post(
            f"/gestion/panel/resultados/{match.pk}/registrar/",
            {"is_walkover": "1", "winner": match.team2_id, "walkover_reason": "lesión"},
            follow=True,
        )
        self.assertContains(response, "Resultado registrado")
        match.refresh_from_db()
        self.assertTrue(match.is_walkover)
        self.assertEqual(match.winner_id, match.team2_id)
        self.assertEqual(match.score, "")
        self.assertEqual(match.recorded_by_id, self.user.pk)

    def test_manual_bracket_generation_button(self):
        self.add_teams(self.tournament, 4)
        zones = self.make_zones(self.tournament, [2, 2])
        for zone in zones:
            match = zone.matches.get(stage="group")
            self.play_match(match, match.team1, score="6-0 6-0")
        self.assertTrue(Match.objects.filter(tournament=self.tournament, stage__in=PLAYOFF_STAGES).exists())
        response = self.client.post("/gestion/panel/llaves/generar/", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Llaves generadas")

    def test_tournament_creation_from_panel_requires_complex(self):
        response = self.client.post(
            "/gestion/nuevo/",
            {
                "complex": self.complex.pk,
                "name": "Nuevo Torneo",
                "category": "tercera",
                "format_type": "zonas_playoffs",
                "start_date": timezone.localdate().isoformat(),
                "best_of_sets": "2",
                "teams_per_zone": "3",
                "qualifiers_per_zone": "2",
                "status": "draft",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        created = Tournament.objects.get(name="Nuevo Torneo")
        self.assertEqual(created.complex_id, self.complex.pk)


class ResultsPanelTests(TournamentFixtureMixin, TestCase):
    """Panel de Resultados: todos los cruces visibles, ordenados y explicados."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("mesa", password="clave-segura-456", is_staff=True)
        self.complex = Complex.objects.create(name="Complejo Resultados", slug="complejo-resultados")
        self.complex.users.add(self.user)
        self.tournament = self.make_tournament(slug="resultados", complex_obj=self.complex)
        self.client.force_login(self.user)

    def _semis_ready(self):
        """Torneo de 4 duplas con zonas cerradas y semifinales generadas."""
        self.add_teams(self.tournament, 4)
        zones = self.make_zones(self.tournament, [2, 2])
        for zone in zones:
            match = zone.matches.get(stage="group")
            self.play_match(match, match.team1, score="6-0 6-0")
        semis = list(
            Match.objects.filter(tournament=self.tournament, stage="semi").order_by("slot")
        )
        self.assertEqual(len(semis), 2, "el cuadro de 4 debe tener dos semifinales")
        return semis

    def test_pending_shows_both_semis_and_final_in_order(self):
        semi1, semi2 = self._semis_ready()
        self.play_match(semi1, semi1.team1, score="6-0 6-0")

        response = self.client.get(f"/gestion/{self.tournament.slug}/resultados/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        # Ambas semifinales visibles: la jugada y la pendiente, identificadas por posición.
        self.assertIn("Semifinal · partido 1", content)
        self.assertIn("Semifinal · partido 2", content)
        # La final aparece en pendientes con la explicación de qué le falta.
        self.assertIn("Ganador de Semifinal 1", content)
        self.assertIn("Semifinal 2: pendiente", content)
        # Orden lógico: primero la semifinal pendiente, después la final.
        semi_pos = content.index("Semifinal <span")
        final_pos = content.index("Final <span")
        self.assertLess(semi_pos, final_pos, "la semifinal pendiente debe listarse antes que la final")
        # La semifinal 2 es jugable desde el panel.
        self.assertIn(f"/gestion/{self.tournament.slug}/resultados/{semi2.pk}/registrar/", content)

    def test_registering_pending_semi_unlocks_final(self):
        semi1, semi2 = self._semis_ready()
        self.play_match(semi1, semi1.team1, score="6-0 6-0")
        semi1.refresh_from_db()
        final = Match.objects.get(tournament=self.tournament, stage="final")

        # Con una sola semif jugada la final no tiene formulario (falta un cruce).
        pending_page = self.client.get(
            f"/gestion/{self.tournament.slug}/resultados/"
        ).content.decode()
        self.assertNotIn(f"/gestion/{self.tournament.slug}/resultados/{final.pk}/registrar/", pending_page)

        response = self.client.post(
            f"/gestion/{self.tournament.slug}/resultados/{semi2.pk}/registrar/",
            {"score": "6-4 6-3"},
            follow=True,
        )
        self.assertContains(response, "Resultado registrado")
        semi2.refresh_from_db()
        final.refresh_from_db()
        self.assertIsNotNone(semi2.winner_id)
        self.assertEqual(
            {final.team1_id, final.team2_id},
            {semi1.winner_id, semi2.winner_id},
            "la final debe completarse con los ganadores de ambas semifinales",
        )
        # Ahora la final es jugable desde el panel.
        content = response.content.decode()
        self.assertIn(f"/gestion/{self.tournament.slug}/resultados/{final.pk}/registrar/", content)

    def test_octavos_rounds_ordered_and_unlock_cuartos(self):
        tournament = self.make_tournament(slug="octavos", complex_obj=self.complex)
        self.add_teams(tournament, 16)
        zones = self.make_zones(tournament, [2] * 8)
        for zone in zones:
            match = zone.matches.get(stage="group")
            self.play_match(match, match.team1, score="6-0 6-0")

        response = self.client.get(f"/gestion/{tournament.slug}/resultados/")
        content = response.content.decode()
        for label in ("Octavos <span", "Cuartos <span", "Semifinal <span", "Final <span"):
            self.assertIn(label, content)
        self.assertLess(content.index("Octavos <span"), content.index("Cuartos <span"))
        self.assertLess(content.index("Cuartos <span"), content.index("Semifinal <span"))
        self.assertLess(content.index("Semifinal <span"), content.index("Final <span"))

        octavos = Match.objects.filter(tournament=tournament, stage="round_of_16").order_by("slot")
        self.assertEqual(octavos.count(), 8)
        for match in octavos:
            self.assertIn(f"/gestion/{tournament.slug}/resultados/{match.pk}/registrar/", content)
        self.assertIn("Ganador de Octavos 1", content)

        first = octavos.first()
        self.client.post(
            f"/gestion/{tournament.slug}/resultados/{first.pk}/registrar/",
            {"score": "6-1 6-1"},
            follow=True,
        )
        cuartos1 = Match.objects.get(tournament=tournament, stage="quarter", slot=1)
        self.assertEqual(cuartos1.team1_id, first.team1_id, "el ganador de Octavos 1 debe ocupar Cuartos 1")

    def test_group_pending_first_then_bracket_rounds(self):
        tournament = self.make_tournament(slug="orden", complex_obj=self.complex)
        self.add_teams(tournament, 6)
        zones = self.make_zones(tournament, [3, 3])
        zone = zones[0]
        matches = list(zone.matches.filter(stage="group").order_by("slot"))
        self.play_match(matches[0], matches[0].team1, score="6-0 6-0")

        response = self.client.get(f"/gestion/{tournament.slug}/resultados/")
        content = response.content.decode()
        self.assertIn("Zona A <span", content)
        self.assertLess(content.index("Zona A <span"), content.index("Zona B <span"))


class PlayerRegistryTests(TournamentFixtureMixin, TestCase):
    """Alta y reutilización de jugadores desde el panel, sin pasar por el admin."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("registro", password="clave-segura-789", is_staff=True)
        self.complex = Complex.objects.create(name="Complejo Registro", slug="complejo-registro")
        self.complex.users.add(self.user)
        self.tournament = self.make_tournament(slug="registro", complex_obj=self.complex)
        self.client.force_login(self.user)

    def test_team_from_names_creates_players_and_reuses_them(self):
        before = Player.objects.count()
        response = self.client.post(
            f"/gestion/{self.tournament.slug}/parejas/crear/",
            {
                "player1_name": "Juan Pérez",
                "player2_name": "Ana Gómez",
                "player1_gender": "M",
                "player2_gender": "F",
            },
            follow=True,
        )
        self.assertContains(response, "Pareja agregada")
        self.assertContains(response, "guardados en la base")
        self.assertEqual(Player.objects.count(), before + 2)
        self.assertEqual(len(players_service.player_ids_in_tournament(self.tournament)), 2)

        other = self.make_tournament(slug="registro-2", complex_obj=self.complex)
        response = self.client.post(
            f"/gestion/{other.slug}/parejas/crear/",
            {"player1_name": "juan perez", "player2_name": "Gómez, Ana"},
            follow=True,
        )
        self.assertContains(response, "Pareja agregada")
        self.assertEqual(Player.objects.count(), before + 2, "los jugadores existentes se reutilizan")
        self.assertEqual(Team.objects.count(), 1, "la dupla también se reutiliza")

    def test_same_player_twice_rejected(self):
        response = self.client.post(
            f"/gestion/{self.tournament.slug}/parejas/crear/",
            {"player1_name": "Juan Pérez", "player2_name": "Pérez, Juan"},
            follow=True,
        )
        self.assertContains(response, "mismo jugador dos veces")
        self.assertEqual(Team.objects.count(), 0)

    def test_player_already_in_tournament_by_name_rejected(self):
        self.client.post(
            f"/gestion/{self.tournament.slug}/parejas/crear/",
            {"player1_name": "Juan Pérez", "player2_name": "Ana Gómez"},
            follow=True,
        )
        response = self.client.post(
            f"/gestion/{self.tournament.slug}/parejas/crear/",
            {"player1_name": "juan perez", "player2_name": "Otro Jugador"},
            follow=True,
        )
        self.assertContains(response, "ya integran otra dupla")
        self.assertEqual(Team.objects.count(), 1)

    def test_single_word_name_asks_for_last_name(self):
        response = self.client.post(
            f"/gestion/{self.tournament.slug}/parejas/crear/",
            {"player1_name": "Juan", "player2_name": "Ana Gómez"},
            follow=True,
        )
        self.assertContains(response, "hace falta apellido y nombre")
        self.assertEqual(Team.objects.count(), 0)

    def test_teams_page_always_shows_form_without_admin_link(self):
        self.client.post(
            f"/gestion/{self.tournament.slug}/parejas/crear/",
            {"player1_name": "Juan Pérez", "player2_name": "Ana Gómez"},
            follow=True,
        )
        response = self.client.get(f"/gestion/{self.tournament.slug}/parejas/")
        self.assertContains(response, 'name="player1_name"')
        self.assertContains(response, 'name="player2_name"')
        self.assertNotContains(response, "/admin/core/player/add/")
        self.assertContains(response, "jugadores guardados en la base")


class PlayersPanelTests(TournamentFixtureMixin, TestCase):
    """Sección Jugadores del panel: listado, edición y unificación de duplicados."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("padron", password="clave-segura-456", is_staff=True)
        self.other_user = User.objects.create_user("ajeno-padron", password="clave-segura-987", is_staff=True)
        self.complex = Complex.objects.create(name="Complejo Padrón", slug="complejo-padron")
        self.other_complex = Complex.objects.create(name="Complejo Otro", slug="complejo-otro")
        self.complex.users.add(self.user)
        self.other_complex.users.add(self.other_user)
        self.tournament = self.make_tournament(slug="padron", complex_obj=self.complex)
        self.client.force_login(self.user)

    def _player(self, first, last, **kwargs):
        return Player.objects.create(first_name=first, last_name=last, gender=kwargs.pop("gender", "M"), **kwargs)

    def test_section_lists_searches_and_marks_current_tournament(self):
        juan = self._player("Juan", "Pérez")
        partner = self._player("Luis", "Díaz")
        self._player("Ana", "Gómez")
        inactive = self._player("Inés", "Inactiva", active=False)
        players_service.create_team(self.tournament, juan, partner)

        response = self.client.get("/gestion/padron/jugadores/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pérez")
        self.assertContains(response, "Gómez")
        self.assertContains(response, "En este torneo")
        self.assertContains(response, 'href="/gestion/padron/jugadores/"')

        # Búsqueda sin acentos y en cualquier orden del nombre.
        response = self.client.get("/gestion/padron/jugadores/", {"q": "perez"})
        self.assertContains(response, "Listado (1)")
        self.assertContains(response, "Pérez")
        self.assertNotContains(response, "Gómez")
        response = self.client.get("/gestion/padron/jugadores/", {"q": "gomez ana"})
        self.assertContains(response, "Listado (1)")
        self.assertContains(response, "Gómez")
        self.assertNotContains(response, f'href="/gestion/padron/jugadores/{juan.pk}/"')

        # Filtro por estado.
        response = self.client.get("/gestion/padron/jugadores/", {"estado": "inactivos"})
        self.assertContains(response, "Inactiva")
        self.assertNotContains(response, "Gómez")
        self.assertFalse(TournamentPlayer.objects.filter(player=inactive).exists())

    def test_player_create_and_duplicate_guard(self):
        response = self.client.post(
            "/gestion/padron/jugadores/crear/",
            {"first_name": "Sara", "last_name": "López", "gender": "F", "level": "sexta", "phone": "11 5555-0000"},
            follow=True,
        )
        self.assertContains(response, "agregado a la base")
        sara = Player.objects.get(last_name="López")
        self.assertEqual((sara.first_name, sara.gender, sara.level, sara.phone), ("Sara", "F", "sexta", "11 5555-0000"))

        response = self.client.post(
            "/gestion/padron/jugadores/crear/",
            {"first_name": "sara", "last_name": "lopez"},
            follow=True,
        )
        self.assertContains(response, "Ya existe")
        self.assertEqual(Player.objects.filter(last_name__in=["López", "lopez"]).count(), 1)

        response = self.client.post("/gestion/padron/jugadores/crear/", {"first_name": "", "last_name": ""}, follow=True)
        self.assertContains(response, "nombre y apellido")

    def test_player_edit_updates_fields_and_availability(self):
        player = self._player("Juan", "Pérez")
        response = self.client.post(
            f"/gestion/padron/jugadores/{player.pk}/",
            {"first_name": "Juan Carlos", "last_name": "Pérez", "gender": "M", "level": "cuarta", "phone": "11 4444-1111", "email": "juan@correo.com"},
            follow=True,
        )
        self.assertContains(response, "actualizados")
        player.refresh_from_db()
        self.assertEqual(player.first_name, "Juan Carlos")
        self.assertEqual(player.level, "cuarta")
        self.assertEqual(player.phone, "11 4444-1111")
        self.assertEqual(player.email, "juan@correo.com")
        self.assertFalse(player.active, "sin tilde de disponibilidad queda inactivo")

        response = self.client.post(
            f"/gestion/padron/jugadores/{player.pk}/",
            {"first_name": "Juan Carlos", "last_name": "Pérez", "gender": "M", "level": "cuarta", "active": "1"},
            follow=True,
        )
        player.refresh_from_db()
        self.assertTrue(player.active)

        response = self.client.post(
            f"/gestion/padron/jugadores/{player.pk}/",
            {"first_name": " ", "last_name": "Pérez"},
            follow=True,
        )
        self.assertContains(response, "nombre y apellido")
        player.refresh_from_db()
        self.assertEqual(player.first_name, "Juan Carlos")

    def test_merge_unused_duplicate(self):
        target = self._player("Juan", "Pérez", level="cuarta")
        duplicate = self._player("juan", "Perez")
        response = self.client.post(
            f"/gestion/padron/jugadores/{duplicate.pk}/unir/", {"target": target.pk}, follow=True
        )
        self.assertContains(response, "se unificó")
        self.assertFalse(Player.objects.filter(pk=duplicate.pk).exists())
        self.assertTrue(Player.objects.filter(pk=target.pk).exists())

    def test_merge_requires_picking_target(self):
        player = self._player("Juan", "Pérez")
        response = self.client.post(f"/gestion/padron/jugadores/{player.pk}/unir/", {"target": ""}, follow=True)
        self.assertContains(response, "Elegí con qué jugador")
        self.assertTrue(Player.objects.filter(pk=player.pk).exists())

        response = self.client.post(
            f"/gestion/padron/jugadores/{player.pk}/unir/", {"target": player.pk}, follow=True
        )
        self.assertContains(response, "distintos")
        self.assertTrue(Player.objects.filter(pk=player.pk).exists())

    def test_merge_moves_teams_registrations_and_matches(self):
        partner = self._player("Luis", "Díaz")
        duplicate = self._player("juan", "Perez")
        real = self._player("Juan", "Pérez")
        tournament_a = self.tournament
        team_a = players_service.create_team(tournament_a, duplicate, partner)
        self.add_team(tournament_a)
        zones = self.make_zones(tournament_a, [2])
        zone = zones[0]
        match = Match.objects.get(tournament=tournament_a, stage="group", zone=zone)
        self.play_match(match, team_a)

        tournament_b = self.make_tournament(slug="padron-b", complex_obj=self.complex)
        team_b = players_service.create_team(tournament_b, real, partner)
        self.assertNotEqual(team_a.pk, team_b.pk)

        response = self.client.post(
            f"/gestion/padron/jugadores/{duplicate.pk}/unir/", {"target": real.pk}, follow=True
        )
        self.assertContains(response, "se unificó")
        self.assertFalse(Player.objects.filter(pk=duplicate.pk).exists())
        self.assertFalse(Team.objects.filter(pk=team_a.pk).exists(), "la dupla duplicada se drena")
        self.assertTrue(Registration.objects.filter(tournament=tournament_a, team=team_b).exists())
        self.assertTrue(Registration.objects.filter(tournament=tournament_b, team=team_b).exists())
        match.refresh_from_db()
        self.assertIn(team_b.pk, (match.team1_id, match.team2_id))
        self.assertEqual(match.winner_id, team_b.pk)

        entry_a = TournamentPlayer.objects.get(tournament=tournament_a, player=real)
        self.assertEqual(entry_a.team_id, team_b.pk)
        self.assertFalse(TournamentPlayer.objects.filter(player_id=duplicate.pk).exists())
        self.assertEqual(TournamentPlayer.objects.filter(tournament=tournament_a, player=real).count(), 1)

        # La zona sigue siendo válida y la tabla muestra a la dupla que se conservó.
        self.assertTrue(draws.is_zone_complete(zone))
        rows = draws.basic_zone_standings(zone)
        self.assertIn(team_b.pk, {row["team"].pk for row in rows})
        winner_row = next(row for row in rows if row["team"].pk == team_b.pk)
        self.assertEqual(winner_row["won"], 1)

        # El panel de clasificación sigue respondiendo y muestra el apellido real.
        response = self.client.get("/gestion/padron/clasificacion/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pérez")

    def test_merge_blocked_when_both_play_same_tournament(self):
        duplicate = self._player("juan", "Perez")
        real = self._player("Juan", "Pérez")
        players_service.create_team(self.tournament, duplicate, self._player("Luis", "Díaz"))
        players_service.create_team(self.tournament, real, self._player("Ana", "Gómez"))
        response = self.client.post(
            f"/gestion/padron/jugadores/{duplicate.pk}/unir/", {"target": real.pk}, follow=True
        )
        self.assertContains(response, "mismo torneo")
        self.assertTrue(Player.objects.filter(pk=duplicate.pk).exists())
        self.assertTrue(Player.objects.filter(pk=real.pk).exists())

    def test_merge_blocked_when_both_are_partners(self):
        duplicate = self._player("juan", "Perez")
        real = self._player("Juan", "Pérez")
        Team.objects.create(player1=duplicate, player2=real)
        response = self.client.post(
            f"/gestion/padron/jugadores/{duplicate.pk}/unir/", {"target": real.pk}, follow=True
        )
        self.assertContains(response, "misma dupla")
        self.assertTrue(Player.objects.filter(pk=duplicate.pk).exists())
        self.assertTrue(Player.objects.filter(pk=real.pk).exists())

    def test_section_denied_for_other_complex(self):
        player = self._player("Juan", "Pérez")
        self.client.force_login(self.other_user)
        for path in [
            "/gestion/padron/jugadores/",
            f"/gestion/padron/jugadores/{player.pk}/",
        ]:
            self.assertEqual(self.client.get(path).status_code, 403, path)
        response = self.client.post(
            f"/gestion/padron/jugadores/{player.pk}/unir/", {"target": player.pk}
        )
        self.assertEqual(response.status_code, 403)


class TournamentDeleteTests(TournamentFixtureMixin, TestCase):
    """Eliminar torneo conservando jugadores y duplas en la base."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("borrador", password="clave-segura-321", is_staff=True)
        self.other_user = User.objects.create_user("ajeno", password="clave-segura-654", is_staff=True)
        self.complex = Complex.objects.create(name="Complejo Borrado", slug="complejo-borrado")
        self.other_complex = Complex.objects.create(name="Complejo Ajeno", slug="complejo-ajeno")
        self.complex.users.add(self.user)
        self.other_complex.users.add(self.other_user)
        self.tournament = self.make_tournament(slug="borrar", complex_obj=self.complex)
        self.client.force_login(self.user)

    def test_delete_removes_tournament_and_keeps_players(self):
        self.add_teams(self.tournament, 2)
        self.make_zones(self.tournament, [2])
        players_before = Player.objects.count()
        teams_before = Team.objects.count()
        tournament_pk = self.tournament.pk

        response = self.client.post(f"/gestion/{self.tournament.slug}/eliminar/", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "eliminado")
        self.assertFalse(Tournament.objects.filter(pk=tournament_pk).exists())
        self.assertFalse(Zone.objects.filter(tournament_id=tournament_pk).exists())
        self.assertFalse(Match.objects.filter(tournament_id=tournament_pk).exists())
        self.assertFalse(Registration.objects.filter(tournament_id=tournament_pk).exists())
        self.assertFalse(TournamentPlayer.objects.filter(tournament_id=tournament_pk).exists())
        self.assertEqual(Player.objects.count(), players_before)
        self.assertEqual(Team.objects.count(), teams_before)

    def test_delete_requires_post(self):
        response = self.client.get(f"/gestion/{self.tournament.slug}/eliminar/")
        self.assertEqual(response.status_code, 405)
        self.assertTrue(Tournament.objects.filter(pk=self.tournament.pk).exists())

    def test_delete_denied_for_other_complex(self):
        self.client.force_login(self.other_user)
        response = self.client.post(f"/gestion/{self.tournament.slug}/eliminar/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Tournament.objects.filter(pk=self.tournament.pk).exists())

    def test_delete_clears_session_selection(self):
        self.client.get(f"/gestion/elegir/{self.tournament.slug}/")
        self.assertEqual(self.client.session.get("padel_tournament_id"), self.tournament.pk)
        self.client.post(f"/gestion/{self.tournament.slug}/eliminar/")
        self.assertIsNone(self.client.session.get("padel_tournament_id"))
        self.assertEqual(self.client.get("/gestion/").status_code, 200)

    def test_delete_option_visible_in_panel(self):
        response = self.client.get(f"/gestion/{self.tournament.slug}/configuracion/")
        self.assertContains(response, "Eliminar torneo")
        response = self.client.get("/gestion/")
        self.assertContains(response, f"/gestion/{self.tournament.slug}/eliminar/")


class PublicViewsTests(TournamentFixtureMixin, TestCase):
    def test_public_list_and_detail(self):
        tournament = self.make_tournament(slug="publico")
        self.add_teams(tournament, 2)
        zone = self.make_zones(tournament, [2])[0]
        match = zone.matches.get(stage="group")
        self.play_match(match, match.team1)
        response = self.client.get("/torneos/")
        self.assertContains(response, "Torneo publico")
        response = self.client.get("/torneos/publico/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, match.team1.display_name)

    def test_public_detail_shows_group_fixture_with_results(self):
        """Las tarjetas de zonas muestran marcador, ganador y partidos pendientes; sin tablas."""
        tournament = self.make_tournament(slug="fixture-publico")
        for name in ("Alfa", "Beta", "Gamma"):
            self.add_team(tournament, name=name)
        zone = self.make_zones(tournament, [3])[0]
        matches = list(zone.matches.filter(stage="group").order_by("slot", "id"))
        self.play_match(matches[0], matches[0].team1)
        response = self.client.get("/torneos/fixture-publico/")
        self.assertContains(response, "Fixture")
        self.assertContains(response, "Fase de grupos")
        self.assertContains(response, f"Zona {zone.name}")
        self.assertContains(response, "6-4 6-3")
        self.assertContains(response, "Pendiente")
        for name in ("Alfa", "Beta", "Gamma"):
            self.assertContains(response, name)
        self.assertNotContains(response, "Posiciones")

    def test_public_bracket_is_graphical_and_goes_to_the_final(self):
        """El cuadro gráfico de eliminatorias va de la primera ronda a la final y marca al ganador."""
        tournament = self.completed_tournament(4, slug="fixture-llaves")
        draws.ensure_playoff_bracket(tournament)
        semi = Match.objects.filter(tournament=tournament, stage="semi").order_by("slot").first()
        self.play_match(semi, semi.team1)
        response = self.client.get("/torneos/fixture-llaves/")
        self.assertContains(response, "Eliminatorias")
        self.assertContains(response, "Semifinal")
        self.assertContains(response, "Final")
        self.assertContains(response, "bracket-canvas")
        self.assertContains(response, "bracket_tree.js")
        self.assertContains(response, "is-winner")
        self.assertContains(response, "data-next=")
        self.assertContains(response, "6-4 6-3")
        self.assertContains(response, "Por definir")
        self.assertNotContains(response, "Posiciones")


class GeneralSettingsTests(TestCase):
    """Configuración general: alta y edición de usuarios del panel."""

    def setUp(self):
        User = get_user_model()
        self.User = User
        self.boss = User.objects.create_superuser("jefe", password="clave-segura-000")
        self.staff = User.objects.create_user("encargado", password="clave-segura-111", is_staff=True)
        self.other_staff = User.objects.create_user("ajeno", password="clave-segura-222", is_staff=True)
        self.regular = User.objects.create_user("sinpanel", password="clave-segura-333")
        self.complex = Complex.objects.create(name="Complejo Uno", slug="complejo-uno")
        self.other_complex = Complex.objects.create(name="Complejo Dos", slug="complejo-dos")
        self.complex.users.add(self.staff, self.regular, self.boss)
        self.other_complex.users.add(self.other_staff)
        self.client.force_login(self.staff)

    def _create_payload(self, **overrides):
        payload = {
            "username": "nuevo.encargado",
            "first_name": "Nuevo",
            "last_name": "Encargado",
            "email": "nuevo@correo.com",
            "password": "clave-larga-999",
            "password2": "clave-larga-999",
            "is_staff": "1",
            "complexes": [self.complex.pk],
        }
        payload.update(overrides)
        return payload

    def test_select_page_links_to_settings(self):
        response = self.client.get("/gestion/")
        self.assertContains(response, "/gestion/configuracion/")

    def test_list_shows_own_complex_users_only(self):
        response = self.client.get("/gestion/configuracion/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "encargado")
        self.assertContains(response, "sinpanel")
        self.assertContains(response, "Vos")
        self.assertContains(response, "Complejo Uno")
        self.assertNotContains(response, "ajeno")
        self.assertNotContains(response, "jefe")

    def test_superuser_sees_all_users(self):
        self.client.force_login(self.boss)
        response = self.client.get("/gestion/configuracion/")
        for username in ["jefe", "encargado", "ajeno", "sinpanel"]:
            self.assertContains(response, username)

    def test_create_user_with_password_and_complex(self):
        response = self.client.post("/gestion/configuracion/usuarios/crear/", self._create_payload(), follow=True)
        self.assertContains(response, "Usuario creado")
        account = self.User.objects.get(username="nuevo.encargado")
        self.assertTrue(account.is_staff)
        self.assertFalse(account.is_superuser)
        self.assertTrue(account.is_active)
        self.assertTrue(account.check_password("clave-larga-999"))
        self.assertTrue(account.complexes.filter(pk=self.complex.pk).exists())
        self.assertTrue(response.redirect_chain[-1][0].endswith(f"/gestion/configuracion/usuarios/{account.pk}/"))

    def test_create_requires_complex_and_valid_password(self):
        response = self.client.post(
            "/gestion/configuracion/usuarios/crear/", self._create_payload(complexes=[]), follow=True
        )
        self.assertContains(response, "al menos un complejo")
        self.assertFalse(self.User.objects.filter(username="nuevo.encargado").exists())

        response = self.client.post(
            "/gestion/configuracion/usuarios/crear/",
            self._create_payload(password="corta", password2="otra"),
            follow=True,
        )
        self.assertContains(response, "no coinciden")
        self.assertFalse(self.User.objects.filter(username="nuevo.encargado").exists())

        response = self.client.post(
            "/gestion/configuracion/usuarios/crear/",
            self._create_payload(password="corta1", password2="corta1"),
            follow=True,
        )
        self.assertContains(response, "alert--error")
        self.assertFalse(self.User.objects.filter(username="nuevo.encargado").exists())

        self.User.objects.create_user("repetido", password="clave-larga-000")
        response = self.client.post(
            "/gestion/configuracion/usuarios/crear/", self._create_payload(username="repetido"), follow=True
        )
        self.assertContains(response, "alert--error")
        self.assertEqual(self.User.objects.filter(username="repetido").count(), 1)

    def test_create_cannot_escalate_or_assign_foreign_complex(self):
        response = self.client.post(
            "/gestion/configuracion/usuarios/crear/",
            self._create_payload(complexes=[self.complex.pk, self.other_complex.pk]),
            follow=True,
        )
        self.assertContains(response, "Sólo podés asignar complejos")
        self.assertFalse(self.User.objects.filter(username="nuevo.encargado").exists())

        response = self.client.post(
            "/gestion/configuracion/usuarios/crear/", self._create_payload(is_superuser="1"), follow=True
        )
        self.assertContains(response, "Usuario creado")
        account = self.User.objects.get(username="nuevo.encargado")
        self.assertFalse(account.is_superuser, "un encargado no puede crear administradores generales")

    def test_superuser_can_create_superuser(self):
        self.client.force_login(self.boss)
        response = self.client.post(
            "/gestion/configuracion/usuarios/crear/",
            self._create_payload(username="jefe2", is_superuser="1", complexes=[]),
            follow=True,
        )
        self.assertContains(response, "Usuario creado")
        account = self.User.objects.get(username="jefe2")
        self.assertTrue(account.is_superuser)

    def test_edit_updates_names_email_and_password(self):
        response = self.client.post(
            f"/gestion/configuracion/usuarios/{self.regular.pk}/",
            {
                "username": "sinpanel",
                "first_name": "Renombrado",
                "last_name": "Apellido",
                "email": "renombrado@correo.com",
                "password": "otra-clave-larga-77",
                "password2": "otra-clave-larga-77",
                "is_active": "1",
                "is_staff": "1",
                "complexes": [self.complex.pk],
            },
            follow=True,
        )
        self.assertContains(response, "actualizado")
        self.regular.refresh_from_db()
        self.assertEqual(self.regular.first_name, "Renombrado")
        self.assertEqual(self.regular.email, "renombrado@correo.com")
        self.assertTrue(self.regular.is_staff)
        self.assertTrue(self.regular.check_password("otra-clave-larga-77"))

    def test_edit_deactivates_and_blocks_login(self):
        response = self.client.post(
            f"/gestion/configuracion/usuarios/{self.regular.pk}/",
            {"username": "sinpanel", "is_staff": "1", "complexes": [self.complex.pk]},
            follow=True,
        )
        self.assertContains(response, "actualizado")
        self.regular.refresh_from_db()
        self.assertFalse(self.regular.is_active)
        self.assertFalse(self.client.login(username="sinpanel", password="clave-segura-333"))

    def test_edit_cannot_remove_last_complex(self):
        response = self.client.post(
            f"/gestion/configuracion/usuarios/{self.regular.pk}/",
            {"username": "sinpanel", "is_active": "1", "is_staff": "1"},
            follow=True,
        )
        self.assertContains(response, "al menos un complejo")
        self.regular.refresh_from_db()
        self.assertTrue(self.regular.complexes.filter(pk=self.complex.pk).exists())

    def test_edit_denied_for_other_complex_user_and_superuser(self):
        self.assertEqual(self.client.get(f"/gestion/configuracion/usuarios/{self.other_staff.pk}/").status_code, 403)
        self.assertEqual(self.client.get(f"/gestion/configuracion/usuarios/{self.boss.pk}/").status_code, 403)
        self.assertEqual(
            self.client.post(f"/gestion/configuracion/usuarios/{self.other_staff.pk}/", {}).status_code, 403
        )

    def test_self_edit_keeps_permissions_active_and_complexes(self):
        response = self.client.post(
            f"/gestion/configuracion/usuarios/{self.staff.pk}/",
            {
                "username": "encargado",
                "first_name": "Encar",
                "last_name": "Gado",
                "email": "enc@correo.com",
                "complexes": [self.complex.pk],
            },
            follow=True,
        )
        self.assertContains(response, "actualizado")
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.first_name, "Encar")
        self.assertTrue(self.staff.is_active, "no puede desactivarse a sí mismo")
        self.assertTrue(self.staff.is_staff, "no puede quitarse el acceso al panel")
        self.assertTrue(self.staff.complexes.filter(pk=self.complex.pk).exists())

    def test_superuser_can_edit_user_of_another_complex(self):
        self.client.force_login(self.boss)
        response = self.client.post(
            f"/gestion/configuracion/usuarios/{self.other_staff.pk}/",
            {"username": "ajeno", "is_active": "1", "is_staff": "1", "complexes": [self.other_complex.pk]},
            follow=True,
        )
        self.assertContains(response, "actualizado")
        self.other_staff.refresh_from_db()
        self.assertTrue(self.other_staff.is_active)

    def test_regular_user_without_panel_is_denied(self):
        self.client.force_login(self.regular)
        self.assertEqual(self.client.get("/gestion/configuracion/").status_code, 403)
        self.assertEqual(self.client.post("/gestion/configuracion/usuarios/crear/", {}).status_code, 403)

    def test_staff_creates_own_complex(self):
        response = self.client.post(
            "/gestion/configuracion/complejos/crear/",
            {"name": "Complejo Nuevo", "city": "Rosario", "address": "", "phone": "", "email": ""},
            follow=True,
        )
        self.assertContains(response, "Complejo creado")
        self.assertContains(response, "Complejo Nuevo")
        complex_obj = Complex.objects.get(name="Complejo Nuevo")
        self.assertTrue(complex_obj.users.filter(pk=self.staff.pk).exists())
        self.assertEqual(complex_obj.slug, "complejo-nuevo")
        # Nombre repetido: se genera un slug distinto.
        self.client.post(
            "/gestion/configuracion/complejos/crear/", {"name": "Complejo Nuevo"}, follow=True
        )
        self.assertEqual(Complex.objects.filter(name="Complejo Nuevo").count(), 2)
        slugs = set(Complex.objects.filter(name="Complejo Nuevo").values_list("slug", flat=True))
        self.assertEqual(slugs, {"complejo-nuevo", "complejo-nuevo-2"})

    def test_staff_edits_own_complex_but_not_foreign(self):
        response = self.client.post(
            f"/gestion/configuracion/complejos/{self.complex.pk}/",
            {"name": "Complejo Uno (editado)", "city": "CABA", "address": "Calle 1", "phone": "", "email": ""},
            follow=True,
        )
        self.assertContains(response, "actualizados")
        self.complex.refresh_from_db()
        self.assertEqual(self.complex.name, "Complejo Uno (editado)")
        response = self.client.post(
            f"/gestion/configuracion/complejos/{self.other_complex.pk}/",
            {"name": "Ajeno", "city": "", "address": "", "phone": "", "email": ""},
        )
        self.assertEqual(response.status_code, 403)
        self.other_complex.refresh_from_db()
        self.assertEqual(self.other_complex.name, "Complejo Dos")

    def test_superuser_can_edit_any_complex(self):
        self.client.force_login(self.boss)
        response = self.client.post(
            f"/gestion/configuracion/complejos/{self.other_complex.pk}/",
            {"name": "Complejo Dos (jefe)", "city": "", "address": "", "phone": "", "email": ""},
            follow=True,
        )
        self.assertContains(response, "actualizados")
        self.other_complex.refresh_from_db()
        self.assertEqual(self.other_complex.name, "Complejo Dos (jefe)")

    def test_regular_user_cannot_create_complex(self):
        self.client.force_login(self.regular)
        self.assertEqual(self.client.post("/gestion/configuracion/complejos/crear/", {"name": "X"}).status_code, 403)

    def test_user_without_complex_creates_one_and_then_can_manage_tournaments(self):
        solo = self.User.objects.create_user("solo", password="clave-segura-444", is_staff=True)
        self.client.force_login(solo)
        response = self.client.get("/gestion/nuevo/", follow=True)
        self.assertContains(response, "Creá el tuyo")
        response = self.client.post(
            "/gestion/configuracion/complejos/crear/", {"name": "Mi Club"}, follow=True
        )
        self.assertContains(response, "Complejo creado")
        response = self.client.get("/gestion/nuevo/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mi Club")


class TournamentCreateDefaultsTests(TestCase):
    """Al crear un torneo: complejo preseleccionado (si hay uno solo), mejor de 3 y estado Inscripciones."""

    def test_single_complex_and_defaults_are_preselected(self):
        User = get_user_model()
        user = User.objects.create_user("unico", password="clave-segura-123", is_staff=True)
        complex_obj = Complex.objects.create(name="Complejo Único", slug="complejo-unico")
        complex_obj.users.add(user)
        self.client.force_login(user)
        response = self.client.get("/gestion/nuevo/")
        self.assertContains(response, f'value="{complex_obj.pk}" selected')
        self.assertContains(response, "único complejo a tu cargo")
        self.assertContains(response, 'value="3" selected')  # mejor de 3 sets
        self.assertContains(response, 'value="open" selected')  # estado Inscripciones

    def test_defaults_are_used_if_fields_missing(self):
        User = get_user_model()
        user = User.objects.create_user("unico2", password="clave-segura-123", is_staff=True)
        complex_obj = Complex.objects.create(name="Complejo Dos", slug="complejo-dos")
        complex_obj.users.add(user)
        self.client.force_login(user)
        response = self.client.post(
            "/gestion/nuevo/",
            {"name": "Torneo Sin Campo", "start_date": timezone.localdate().isoformat()},
            follow=True,
        )
        self.assertContains(response, "Torneo creado")
        tournament = Tournament.objects.get(name="Torneo Sin Campo")
        self.assertEqual(tournament.complex_id, complex_obj.pk)
        self.assertEqual(tournament.best_of_sets, 3)
        self.assertEqual(tournament.status, "open")

    def test_multiple_complexes_are_not_preselected(self):
        User = get_user_model()
        user = User.objects.create_user("doble", password="clave-segura-123", is_staff=True)
        first = Complex.objects.create(name="Complejo A", slug="complejo-a")
        second = Complex.objects.create(name="Complejo B", slug="complejo-b")
        first.users.add(user)
        second.users.add(user)
        self.client.force_login(user)
        html = self.client.get("/gestion/nuevo/").content.decode()
        select_block = re.search(r'<select name="complex" required>(.*?)</select>', html, re.S).group(1)
        self.assertNotIn("selected", select_block)

    def test_editing_keeps_saved_values(self):
        """Al editar se mantienen los valores guardados, no los defaults de creación."""
        User = get_user_model()
        user = User.objects.create_user("edita", password="clave-segura-123", is_staff=True)
        complex_obj = Complex.objects.create(name="Complejo Edit", slug="complejo-edit")
        complex_obj.users.add(user)
        tournament = Tournament.objects.create(
            complex=complex_obj,
            name="Torneo Edit",
            slug="torneo-edit",
            category="primera",
            start_date=timezone.localdate(),
            best_of_sets=2,
            status="running",
        )
        self.client.force_login(user)
        response = self.client.get(f"/gestion/{tournament.slug}/configuracion/")
        self.assertContains(response, 'value="2" selected')
        self.assertContains(response, 'value="running" selected')


class InvitationFlowTests(TestCase):
    """Invitaciones por email: alta, revocación y registro con Google."""

    def setUp(self):
        User = get_user_model()
        self.User = User
        self.boss = User.objects.create_superuser("jefe", password="clave-segura-000")
        self.staff = User.objects.create_user(
            "encargado", password="clave-segura-111", is_staff=True, email="ocupado@gmail.com"
        )
        self.complex = Complex.objects.create(name="Complejo Uno", slug="complejo-uno")
        self.complex.users.add(self.staff, self.boss)
        self.client.force_login(self.boss)

    def _sociallogin(self, email="dueno@gmail.com", first_name="Futuro", last_name="Dueno", uid="u-1"):
        from allauth.socialaccount.adapter import get_adapter as get_social_adapter

        provider = get_social_adapter().get_provider(RequestFactory().get("/"), provider="google")
        user = self.User(email=email, first_name=first_name, last_name=last_name)
        sociallogin = SocialLogin(
            provider=provider,
            account=SocialAccount(provider="google", uid=uid),
            user=user,
        )
        sociallogin.email_addresses = [EmailAddress(email=email, verified=True)]
        return sociallogin

    def _create_invitation(self, email="dueno@gmail.com", **kwargs):
        return Invitation.objects.create(email=email, created_by=self.boss, **kwargs)

    def test_only_superuser_manages_invitations(self):
        self.client.force_login(self.staff)
        response = self.client.post("/gestion/configuracion/invitaciones/crear/", {"email": "x@gmail.com"})
        self.assertEqual(response.status_code, 403)
        invitation = self._create_invitation()
        response = self.client.post(f"/gestion/configuracion/invitaciones/{invitation.pk}/eliminar/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Invitation.objects.filter(pk=invitation.pk).exists())

    def test_create_invitation_normalizes_and_validates(self):
        response = self.client.post(
            "/gestion/configuracion/invitaciones/crear/", {"email": " Dueno@Gmail.com "}, follow=True
        )
        self.assertContains(response, "Invitación guardada")
        invitation = Invitation.objects.get()
        self.assertEqual(invitation.email, "dueno@gmail.com")
        self.assertIsNone(invitation.complex)
        self.assertEqual(invitation.created_by, self.boss)

        response = self.client.post(
            "/gestion/configuracion/invitaciones/crear/", {"email": "DUENO@gmail.com"}, follow=True
        )
        self.assertContains(response, "ya tiene una invitación pendiente")
        self.assertEqual(Invitation.objects.count(), 1)

        response = self.client.post(
            "/gestion/configuracion/invitaciones/crear/", {"email": "ocupado@gmail.com"}, follow=True
        )
        self.assertContains(response, "Ya existe un usuario")
        self.assertEqual(Invitation.objects.count(), 1)

        response = self.client.post(
            "/gestion/configuracion/invitaciones/crear/", {"email": "sin-arroba"}, follow=True
        )
        self.assertContains(response, "formato válido")
        self.assertEqual(Invitation.objects.count(), 1)

    def test_create_invitation_with_complex(self):
        response = self.client.post(
            "/gestion/configuracion/invitaciones/crear/",
            {"email": "personal@gmail.com", "complex": str(self.complex.pk)},
            follow=True,
        )
        self.assertContains(response, "Invitación guardada")
        self.assertEqual(Invitation.objects.get().complex, self.complex)

    def test_revoke_pending_invitation(self):
        invitation = self._create_invitation()
        response = self.client.post(
            f"/gestion/configuracion/invitaciones/{invitation.pk}/eliminar/", follow=True
        )
        self.assertContains(response, "Invitación revocada")
        self.assertFalse(Invitation.objects.exists())

    def test_settings_page_shows_section_only_for_superuser(self):
        self._create_invitation()
        response = self.client.get("/gestion/configuracion/")
        self.assertContains(response, "Invitaciones por email")
        self.assertContains(response, "dueno@gmail.com")
        self.client.force_login(self.staff)
        response = self.client.get("/gestion/configuracion/")
        self.assertNotContains(response, "Invitaciones por email")
        self.assertNotContains(response, "dueno@gmail.com")

    def test_adapter_opens_signup_only_for_invited_verified_email(self):
        adapter = PadelSocialAccountAdapter()
        request = RequestFactory().get("/")
        self.assertFalse(adapter.is_open_for_signup(request, self._sociallogin("nadie@gmail.com")))
        self._create_invitation()
        self.assertTrue(adapter.is_open_for_signup(request, self._sociallogin()))
        unverified = self._sociallogin()
        unverified.email_addresses = [EmailAddress(email="dueno@gmail.com", verified=False)]
        self.assertFalse(adapter.is_open_for_signup(request, unverified))

    def test_auto_signup_only_when_invitation_has_complex(self):
        adapter = PadelSocialAccountAdapter()
        request = RequestFactory().get("/")
        self._create_invitation()
        self.assertFalse(adapter.is_auto_signup_allowed(request, self._sociallogin()))
        Invitation.objects.update(complex=self.complex)
        self.assertTrue(adapter.is_auto_signup_allowed(request, self._sociallogin()))

    def test_google_signup_closed_without_invitation(self):
        self.client.logout()
        session = self.client.session
        session["socialaccount_sociallogin"] = self._sociallogin("nadie@gmail.com").serialize()
        session.save()
        response = self.client.get("/cuentas/3rdparty/signup/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Registro cerrado")
        self.assertNotContains(response, "Completar registro")
        self.assertFalse(self.User.objects.filter(email="nadie@gmail.com").exists())

    def test_google_form_completes_signup_and_marks_invitation_used(self):
        invitation = self._create_invitation()
        self.client.logout()
        session = self.client.session
        session["socialaccount_sociallogin"] = self._sociallogin().serialize()
        session.save()

        response = self.client.get("/cuentas/3rdparty/signup/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Completar registro")
        self.assertContains(response, "dueno@gmail.com")
        self.assertContains(response, "Futuro")

        response = self.client.post(
            "/cuentas/3rdparty/signup/",
            {"first_name": "Futuro", "last_name": "Dueno", "username": "futuro.dueno"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("general_settings"))
        account = self.User.objects.get(username="futuro.dueno")
        self.assertTrue(account.is_staff)
        self.assertTrue(account.is_active)
        self.assertEqual(account.email, "dueno@gmail.com")
        self.assertEqual(account.last_name, "Dueno")
        self.assertFalse(account.has_usable_password())
        self.assertTrue(
            SocialAccount.objects.filter(provider="google", uid="u-1", user=account).exists()
        )
        self.assertTrue(EmailAddress.objects.filter(user=account, email="dueno@gmail.com").exists())
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.used_at)
        self.assertEqual(invitation.used_by, account)
        self.assertEqual(int(self.client.session["_auth_user_id"]), account.pk)

    def test_auto_signup_with_complex_assigns_complex_and_redirects(self):
        from django.contrib.sessions.middleware import SessionMiddleware

        invitation = self._create_invitation(email="personal@gmail.com", complex=self.complex)
        sociallogin = self._sociallogin(
            email="personal@gmail.com", first_name="Personal", last_name="Nuevo", uid="u-2"
        )
        adapter = PadelSocialAccountAdapter()
        request = RequestFactory().get("/")
        SessionMiddleware(lambda r: None).process_request(request)
        adapter.populate_user(
            request,
            sociallogin,
            {"email": sociallogin.user.email, "first_name": "Personal", "last_name": "Nuevo"},
        )
        user = adapter.save_user(request, sociallogin, form=None)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.complexes.filter(pk=self.complex.pk).exists())
        self.assertEqual(sociallogin.state["next"], reverse("tournament_select"))
        invitation.refresh_from_db()
        self.assertEqual(invitation.used_by, user)
        self.assertIsNotNone(invitation.used_at)
        self.assertTrue(
            SocialAccount.objects.filter(provider="google", uid="u-2", user=user).exists()
        )


class SignupRequestTests(TestCase):
    """Registro sin Google: solicitud pública, aviso por email y contraseña temporal."""

    def setUp(self):
        User = get_user_model()
        self.User = User
        self.boss = User.objects.create_superuser("jefe", password="clave-segura-000")
        self.staff = User.objects.create_user("encargado", password="clave-segura-111", is_staff=True)
        self.complex = Complex.objects.create(name="Complejo Uno", slug="complejo-uno")
        self.complex.users.add(self.staff, self.boss)

    def _create_payload(self, **overrides):
        payload = {
            "username": "nuevo.panel",
            "first_name": "Nuevo",
            "last_name": "Panel",
            "email": "nuevo@correo.com",
            "password": "clave-larga-999",
            "password2": "clave-larga-999",
            "is_staff": "1",
            "complexes": [self.complex.pk],
        }
        payload.update(overrides)
        return payload

    def _signup_payload(self, **overrides):
        payload = {
            "email": "nuevo.dueno@correo.com",
            "first_name": "Nuevo",
            "last_name": "Dueño",
            "phone": "+54 9 341 555 5555",
            "complex_name": "Complejo Nuevo",
            "complex_city": "Rosario",
            "complex_address": "Av. Siempreviva 742",
        }
        payload.update(overrides)
        return payload

    def test_login_page_links_to_signup_request(self):
        response = self.client.get("/cuentas/login/")
        self.assertContains(response, "Registrarse")
        self.assertContains(response, "/registrarse/")

    def test_signup_request_saves_applicant_and_complex_data(self):
        response = self.client.post(
            "/registrarse/", self._signup_payload(email=" Nuevo.Dueno@Correo.com "), follow=True
        )
        self.assertContains(response, "Te vamos a avisar")
        solicitud = RegistrationRequest.objects.get()
        self.assertEqual(solicitud.email, "nuevo.dueno@correo.com")
        self.assertEqual(solicitud.first_name, "Nuevo")
        self.assertEqual(solicitud.last_name, "Dueño")
        self.assertEqual(solicitud.phone, "+54 9 341 555 5555")
        self.assertEqual(solicitud.complex_name, "Complejo Nuevo")
        self.assertEqual(solicitud.complex_city, "Rosario")
        self.assertEqual(solicitud.complex_address, "Av. Siempreviva 742")

    @override_settings(WHATSAPP_ALERT_PHONE="+5493415555555", WHATSAPP_ALERT_APIKEY="clave")
    def test_signup_request_validates_data_duplicates_and_existing_users(self):
        RegistrationRequest.objects.create(email="repetido@correo.com")
        self.staff.email = "ocupado@correo.com"
        self.staff.save()
        with patch("core.notifications.urlopen") as fake:
            response = self.client.post(
                "/registrarse/", self._signup_payload(email="REPETIDO@correo.com"), follow=True
            )
            self.assertContains(response, "Te vamos a avisar")
            self.assertEqual(RegistrationRequest.objects.count(), 1)

            response = self.client.post(
                "/registrarse/", self._signup_payload(email="sin-arroba"), follow=True
            )
            self.assertContains(response, "email válido")
            self.assertEqual(RegistrationRequest.objects.count(), 1)

            response = self.client.post(
                "/registrarse/", self._signup_payload(email="ocupado@correo.com"), follow=True
            )
            self.assertContains(response, "ya tiene una cuenta")
            self.assertEqual(RegistrationRequest.objects.count(), 1)

            response = self.client.post(
                "/registrarse/", self._signup_payload(complex_name=""), follow=True
            )
            self.assertContains(response, "Completá tu nombre")
            self.assertEqual(RegistrationRequest.objects.count(), 1)
        self.assertEqual(fake.call_count, 0)

    @override_settings(WHATSAPP_ALERT_PHONE="+5493415555555", WHATSAPP_ALERT_APIKEY="clave")
    def test_signup_request_honeypot_silently_ignored(self):
        with patch("core.notifications.urlopen") as fake:
            response = self.client.post(
                "/registrarse/",
                self._signup_payload(email="bot@correo.com", website="https://spam"),
                follow=True,
            )
        self.assertContains(response, "Te vamos a avisar")
        self.assertFalse(RegistrationRequest.objects.exists())
        self.assertEqual(fake.call_count, 0)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_signup_request_alerts_admins_by_email(self):
        self.boss.email = "jefe@correo.com"
        self.boss.save()
        response = self.client.post(
            "/registrarse/", self._signup_payload(email="aviso@correo.com"), follow=True
        )
        self.assertContains(response, "Te vamos a avisar")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["jefe@correo.com"])
        self.assertIn("aviso@correo.com", message.subject)
        self.assertIn("Complejo Nuevo", message.body)
        self.assertIn("Solicitudes de registro", message.body)

    @override_settings(WHATSAPP_ALERT_PHONE="+5493415555555", WHATSAPP_ALERT_APIKEY="clave-secreta")
    def test_signup_request_alerts_by_whatsapp(self):
        with patch("core.notifications.urlopen") as fake:
            response = self.client.post(
                "/registrarse/", self._signup_payload(email="wsp@correo.com"), follow=True
            )
        self.assertContains(response, "Te vamos a avisar")
        self.assertEqual(fake.call_count, 1)
        called_url = fake.call_args[0][0]
        self.assertIn("api.callmebot.com/whatsapp.php", called_url)
        self.assertIn("clave-secreta", called_url)
        self.assertIn("wsp%40correo.com", called_url)

    @override_settings(WHATSAPP_ALERT_PHONE="+5493415555555", WHATSAPP_ALERT_APIKEY="clave")
    def test_whatsapp_failure_does_not_break_signup(self):
        with patch("core.notifications.urlopen", side_effect=OSError("sin red")):
            response = self.client.post(
                "/registrarse/", self._signup_payload(email="falla@correo.com"), follow=True
            )
        self.assertContains(response, "Te vamos a avisar")
        self.assertTrue(RegistrationRequest.objects.filter(email="falla@correo.com").exists())

    def test_settings_shows_requests_and_prefills_data_for_superuser(self):
        RegistrationRequest.objects.create(
            email="nuevo@correo.com",
            first_name="Nuevo",
            last_name="Dueño",
            phone="3415555555",
            complex_name="Complejo Nuevo",
            complex_city="Rosario",
            complex_address="Calle 123",
        )
        self.client.force_login(self.boss)
        response = self.client.get("/gestion/configuracion/")
        self.assertContains(response, "Solicitudes de registro")
        self.assertContains(response, "nuevo@correo.com")
        self.assertContains(response, "Nuevo Dueño")
        self.assertContains(response, "3415555555")
        self.assertContains(response, "Complejo Nuevo")
        response = self.client.get(
            "/gestion/configuracion/",
            {
                "email": "nuevo@correo.com",
                "first_name": "Nuevo",
                "last_name": "Dueño",
                "phone": "3415555555",
                "complex_name": "Complejo Nuevo",
                "complex_city": "Rosario",
                "complex_address": "Calle 123",
            },
        )
        self.assertContains(response, 'value="nuevo@correo.com"')
        self.assertContains(response, 'value="Nuevo"')
        self.assertContains(response, 'value="Complejo Nuevo"')
        self.assertContains(response, "Crear el complejo")
        self.client.force_login(self.staff)
        response = self.client.get("/gestion/configuracion/")
        self.assertNotContains(response, "Solicitudes de registro")

    def test_dismiss_request_is_superuser_only(self):
        solicitud = RegistrationRequest.objects.create(email="nuevo@correo.com")
        self.client.force_login(self.staff)
        response = self.client.post(f"/gestion/configuracion/solicitudes/{solicitud.pk}/eliminar/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(RegistrationRequest.objects.filter(pk=solicitud.pk).exists())
        self.client.force_login(self.boss)
        response = self.client.post(
            f"/gestion/configuracion/solicitudes/{solicitud.pk}/eliminar/", follow=True
        )
        self.assertContains(response, "Solicitud descartada")
        self.assertFalse(RegistrationRequest.objects.exists())

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_create_user_from_request_sends_credentials_and_clears_request(self):
        RegistrationRequest.objects.create(
            email="nuevo@correo.com",
            first_name="Nuevo",
            last_name="Panel",
            phone="3415555555",
            complex_name="Complejo Solicitado",
        )
        self.client.force_login(self.boss)
        response = self.client.post(
            "/gestion/configuracion/usuarios/crear/",
            self._create_payload(notify_email="1"),
            follow=True,
        )
        self.assertContains(response, "Se enviaron los datos de acceso")
        self.assertFalse(RegistrationRequest.objects.exists())
        account = self.User.objects.get(username="nuevo.panel")
        self.assertTrue(TemporaryPassword.objects.filter(user=account).exists())
        self.assertFalse(Complex.objects.filter(name="Complejo Solicitado").exists())
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["nuevo@correo.com"])
        self.assertIn("nuevo.panel", message.body)
        self.assertIn("clave-larga-999", message.body)

    def test_create_user_from_request_can_create_and_assign_complex(self):
        RegistrationRequest.objects.create(
            email="dueno@correo.com",
            first_name="Dueño",
            last_name="Nuevo",
            phone="3415555555",
            complex_name="Complejo Request",
            complex_city="Rosario",
            complex_address="Calle 123",
        )
        self.client.force_login(self.boss)
        response = self.client.post(
            "/gestion/configuracion/usuarios/crear/",
            self._create_payload(
                username="dueno.nuevo",
                email="dueno@correo.com",
                complexes=[],
                create_complex="1",
                complex_name="Complejo Request",
                complex_city="Rosario",
                complex_address="Calle 123",
                complex_phone="3415555555",
                complex_email="dueno@correo.com",
            ),
            follow=True,
        )
        self.assertContains(response, "Usuario creado")
        account = self.User.objects.get(username="dueno.nuevo")
        complex_obj = Complex.objects.get(name="Complejo Request")
        self.assertEqual(complex_obj.city, "Rosario")
        self.assertEqual(complex_obj.address, "Calle 123")
        self.assertEqual(complex_obj.slug, "complejo-request")
        self.assertTrue(account.complexes.filter(pk=complex_obj.pk).exists())
        self.assertTrue(complex_obj.users.filter(pk=self.boss.pk).exists())
        self.assertFalse(RegistrationRequest.objects.exists())

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_notify_without_email_warns_and_keeps_user(self):
        self.client.force_login(self.boss)
        response = self.client.post(
            "/gestion/configuracion/usuarios/crear/",
            self._create_payload(email="", notify_email="1"),
            follow=True,
        )
        self.assertContains(response, "no tiene un email cargado")
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(self.User.objects.filter(username="nuevo.panel").exists())
        self.assertTrue(TemporaryPassword.objects.filter(user__username="nuevo.panel").exists())

    def test_admin_password_reset_marks_temporary_and_self_change_clears(self):
        self.client.force_login(self.boss)
        response = self.client.post(
            f"/gestion/configuracion/usuarios/{self.staff.pk}/",
            {
                "username": "encargado",
                "email": "encargado@correo.com",
                "password": "nueva-clave-larga-77",
                "password2": "nueva-clave-larga-77",
                "is_active": "1",
                "is_staff": "1",
                "complexes": [self.complex.pk],
            },
            follow=True,
        )
        self.assertContains(response, "actualizado")
        self.assertTrue(TemporaryPassword.objects.filter(user=self.staff).exists())
        # El objeto en memoria quedó con la contraseña vieja: al refrescar (y con
        # ella la sesión), Django invalida las sesiones previas del usuario.
        self.staff.refresh_from_db()
        # El propio usuario cambia su contraseña desde su ficha: se limpia la marca.
        self.client.force_login(self.staff)
        response = self.client.post(
            f"/gestion/configuracion/usuarios/{self.staff.pk}/",
            {
                "username": "encargado",
                "password": "propia-clave-larga-88",
                "password2": "propia-clave-larga-88",
                "complexes": [self.complex.pk],
            },
            follow=True,
        )
        self.assertContains(response, "actualizado")
        self.assertFalse(TemporaryPassword.objects.filter(user=self.staff).exists())
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password("propia-clave-larga-88"))

    def test_password_policy_only_needs_eight_characters(self):
        self.client.force_login(self.boss)
        response = self.client.post(
            "/gestion/configuracion/usuarios/crear/",
            self._create_payload(username="clave.facil", password="padel2026", password2="padel2026"),
            follow=True,
        )
        self.assertContains(response, "Usuario creado")
        account = self.User.objects.get(username="clave.facil")
        self.assertTrue(account.check_password("padel2026"))
        response = self.client.post(
            "/gestion/configuracion/usuarios/crear/",
            self._create_payload(username="clave.corta", password="corta12", password2="corta12"),
            follow=True,
        )
        self.assertContains(response, "alert--error")
        self.assertFalse(self.User.objects.filter(username="clave.corta").exists())


class PasswordChangeFlowTests(TestCase):
    """Cambio de la contraseña temporal desde el panel (allauth)."""

    def setUp(self):
        User = get_user_model()
        self.User = User
        self.user = User.objects.create_user("temporal", password="clave-temporal-000", is_staff=True)
        TemporaryPassword.objects.create(user=self.user)

    def test_panel_shows_banner_until_password_changes(self):
        self.client.force_login(self.user)
        response = self.client.get("/gestion/configuracion/")
        self.assertContains(response, "Cambiala por una tuya")
        self.assertContains(response, "/cuentas/password/change/")
        self.user.temporary_password.delete()
        response = self.client.get("/gestion/configuracion/")
        self.assertNotContains(response, "Cambiala por una tuya")

    def test_password_change_page_renders(self):
        self.client.force_login(self.user)
        response = self.client.get("/cuentas/password/change/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cambiar contraseña")

    def test_password_change_clears_flag_and_redirects_to_panel(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/cuentas/password/change/",
            {
                "oldpassword": "clave-temporal-000",
                "password1": "mi-clave-propia-77",
                "password2": "mi-clave-propia-77",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("tournament_select"))
        self.assertFalse(TemporaryPassword.objects.exists())
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("mi-clave-propia-77"))


class AuthPagesTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("auth-user", password="clave-segura-123", is_staff=True)

    def test_login_page_renders_and_google_hidden_by_default(self):
        # Sin credenciales cargadas no se muestra el botón, aunque el entorno
        # real tenga GOOGLE_CLIENT_ID/SECRET en el .env.
        with self.settings(GOOGLE_LOGIN_ENABLED=False):
            response = self.client.get("/cuentas/login/")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Entrar")
            self.assertNotContains(response, "Ingresar con Google")

    def test_google_button_appears_when_credentials_loaded(self):
        with self.settings(
            GOOGLE_LOGIN_ENABLED=True,
            SOCIALACCOUNT_PROVIDERS={
                "google": {
                    "APP": {"client_id": "cliente-de-prueba", "secret": "secreto-de-prueba", "key": ""},
                    "SCOPE": ["profile", "email"],
                    "AUTH_PARAMS": {"access_type": "online"},
                }
            },
        ):
            response = self.client.get("/cuentas/login/")
            self.assertContains(response, "Ingresar con Google")
            self.assertContains(response, "/cuentas/google/login/")

    def test_google_login_skips_confirmation_and_goes_to_google(self):
        # Con SOCIALACCOUNT_LOGIN_ON_GET el botón va directo a Google: sin la
        # pantalla intermedia de "iniciar sesión o registrarse / Continuar".
        with self.settings(
            GOOGLE_LOGIN_ENABLED=True,
            SOCIALACCOUNT_PROVIDERS={
                "google": {
                    "APP": {"client_id": "cliente-de-prueba", "secret": "secreto-de-prueba", "key": ""},
                    "SCOPE": ["profile", "email"],
                    "AUTH_PARAMS": {"access_type": "online"},
                }
            },
        ):
            response = self.client.get("/cuentas/google/login/?process=login")
            self.assertEqual(response.status_code, 302)
            self.assertIn("accounts.google.com", response["Location"])
            self.assertIn("cliente-de-prueba", response["Location"])

    def test_signup_closed_page_explains_invitation_only(self):
        response = self.client.get("/cuentas/signup/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Registro cerrado")
        self.assertContains(response, "por invitación")
        self.assertContains(response, "mismo email")

    def test_home_is_public_landing_with_booking_and_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sacar un turno")
        self.assertContains(response, "/turnos/")
        self.assertContains(response, "/cuentas/login/")

    def test_home_shows_panel_link_for_authenticated_users(self):
        self.client.force_login(self.user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Panel de gestión")

    def test_login_with_existing_credentials_still_works(self):
        response = self.client.post(
            "/cuentas/login/", {"login": "auth-user", "password": "clave-segura-123"}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], "/gestion/")

    def test_logout_from_panel_ends_session(self):
        self.client.force_login(self.user)
        response = self.client.post("/cuentas/logout/")
        self.assertEqual(response.status_code, 302)
        response = self.client.get("/gestion/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/cuentas/login/"))


class SelfRegistrationTests(TournamentFixtureMixin, TestCase):
    """Autoinscripción pública de parejas con código y confirmación del complejo."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        User = get_user_model()
        self.user = User.objects.create_user("dueno", password="clave-segura-123", is_staff=True, is_superuser=True)
        self.complex = Complex.objects.create(name="Complejo Código", slug="complejo-codigo")
        self.complex.users.add(self.user)
        self.tournament = self.make_tournament(slug="codigo", complex_obj=self.complex)
        self.tournament.status = "open"
        self.tournament.self_registration_enabled = True
        self.tournament.save(update_fields=["status", "self_registration_enabled"])
        self.code = players_service.ensure_registration_code(self.tournament)
        self.url = "/torneos/codigo/anotarse/"

    def signup_payload(self, **overrides):
        payload = {
            "code": self.code,
            "player1_first_name": "Ana",
            "player1_last_name": "Gómez",
            "player1_gender": "F",
            "player1_phone": "341 555 1111",
            "player2_first_name": "Juana",
            "player2_last_name": "Ferreyra",
            "player2_gender": "F",
            "player2_phone": "",
        }
        payload.update(overrides)
        return payload

    def test_signup_page_visible_only_when_open_and_enabled(self):
        response = self.client.get("/torneos/codigo/")
        self.assertContains(response, "Anotá tu pareja")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Código del torneo")
        response = self.client.get(f"{self.url}?codigo={self.code}")
        self.assertContains(response, self.code)

        self.tournament.status = "running"
        self.tournament.save(update_fields=["status"])
        self.assertNotContains(self.client.get("/torneos/codigo/"), "Anotá tu pareja")
        response = self.client.get(self.url, follow=True)
        self.assertContains(response, "no está abierta")

    def test_signup_creates_pending_pair(self):
        response = self.client.post(self.url, self.signup_payload(), follow=True)
        self.assertContains(response, "pendiente de confirmación")
        registration = Registration.objects.get(tournament=self.tournament)
        self.assertEqual(registration.status, "pending")
        self.assertEqual(registration.source, "public")
        self.assertEqual(registration.team.name, "Gómez - Ferreyra")
        self.assertEqual(registration.team.player1.phone, "341 555 1111")
        # Recién se juega cuando el complejo la confirma.
        self.assertNotIn(registration.team_id, players_service.registration_teams(self.tournament))
        self.assertNotIn(registration.team_id, players_service.tournament_team_ids(self.tournament))

    def test_signup_wrong_code_is_rejected(self):
        response = self.client.post(self.url, self.signup_payload(code="MALCODIGO"), follow=True)
        self.assertContains(response, "El código no es correcto")
        self.assertFalse(Registration.objects.filter(tournament=self.tournament).exists())

    def test_signup_reuses_existing_player_and_blocks_duplicates(self):
        player1 = Player.objects.create(first_name="Juan", last_name="Pérez", gender="M")
        player2 = Player.objects.create(first_name="Luis", last_name="Díaz", gender="M")
        team = Team.objects.create(player1=player1, player2=player2, name="")
        Registration.objects.create(tournament=self.tournament, team=team, status="confirmed")
        players_before = Player.objects.count()
        response = self.client.post(
            self.url,
            self.signup_payload(
                player1_first_name="Juan",
                player1_last_name="Pérez",
                player1_gender="M",
                player2_first_name="Otro",
                player2_last_name="Jugador",
                player2_gender="M",
            ),
            follow=True,
        )
        self.assertContains(response, "ya integran otra dupla")
        self.assertEqual(Player.objects.count(), players_before)
        self.assertEqual(Registration.objects.filter(tournament=self.tournament).count(), 1)

    def test_signup_requires_both_player_names(self):
        response = self.client.post(self.url, self.signup_payload(player2_last_name=""), follow=True)
        self.assertContains(response, "Completá el nombre y apellido de los dos jugadores")
        self.assertFalse(Registration.objects.filter(tournament=self.tournament).exists())

    def test_signup_rate_limit_blocks_after_failures(self):
        for _ in range(_SIGNUP_FAIL_LIMIT):
            self.client.post(self.url, self.signup_payload(code="MALCODIGO"))
        response = self.client.post(self.url, self.signup_payload(), follow=True)
        self.assertContains(response, "Demasiados intentos")
        self.assertFalse(Registration.objects.filter(tournament=self.tournament).exists())

    def test_panel_confirms_and_rejects_pending_pair(self):
        self.client.force_login(self.user)
        player1 = Player.objects.create(first_name="Alta", last_name="Pendiente", gender="M")
        player2 = Player.objects.create(first_name="Baja", last_name="Pendiente", gender="M")
        team = players_service.create_team(
            self.tournament, player1, player2, registration_status="pending", source="public"
        )
        registration = Registration.objects.get(tournament=self.tournament, team=team)
        response = self.client.post(
            f"/gestion/codigo/inscripciones/{registration.pk}/confirmar/", follow=True
        )
        registration.refresh_from_db()
        self.assertEqual(registration.status, "confirmed")
        self.assertIn(team.pk, players_service.registration_teams(self.tournament))
        self.assertContains(response, "quedó confirmada")

        player3 = Player.objects.create(first_name="Recha", last_name="Zada", gender="M")
        player4 = Player.objects.create(first_name="Fuera", last_name="Zada", gender="M")
        team2 = players_service.create_team(
            self.tournament, player3, player4, registration_status="pending", source="public"
        )
        registration2 = Registration.objects.get(tournament=self.tournament, team=team2)
        response = self.client.post(
            f"/gestion/codigo/inscripciones/{registration2.pk}/rechazar/", follow=True
        )
        registration2.refresh_from_db()
        self.assertEqual(registration2.status, "cancelled")
        self.assertNotIn(team2.pk, players_service.registration_teams(self.tournament))
        self.assertContains(response, "Se rechazó la solicitud")

    def test_panel_code_lifecycle(self):
        self.client.force_login(self.user)
        old_code = self.code
        response = self.client.post("/gestion/codigo/autoinscripcion/regenerar/", follow=True)
        self.tournament.refresh_from_db()
        self.assertNotEqual(self.tournament.registration_code, old_code)
        self.assertContains(response, self.tournament.registration_code)
        # Un solo mensaje con el link (que ya lleva el código adentro) y otro extra para mandar sólo el código.
        share_message = (
            "Anotá tu pareja al torneo «Torneo codigo» 🎾\n"
            f"http://testserver/torneos/codigo/anotarse/?codigo={self.tournament.registration_code}"
        )
        self.assertContains(response, "Compartir por WhatsApp")
        self.assertContains(response, "Enviar solo el código")
        self.assertContains(response, "https://wa.me/?text=" + quote(share_message))
        self.assertContains(
            response, "https://wa.me/?text=" + quote(self.tournament.registration_code)
        )

        self.client.post("/gestion/codigo/autoinscripcion/desactivar/", follow=True)
        self.tournament.refresh_from_db()
        self.assertFalse(self.tournament.self_registration_enabled)

        self.client.post("/gestion/codigo/autoinscripcion/activar/", follow=True)
        self.tournament.refresh_from_db()
        self.assertTrue(self.tournament.self_registration_enabled)
        self.assertTrue(self.tournament.registration_code)
        response = self.client.post(self.url, self.signup_payload(code=old_code), follow=True)
        self.assertContains(response, "El código no es correcto")

    def test_panel_self_registration_requires_login(self):
        response = self.client.post("/gestion/codigo/autoinscripcion/activar/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/cuentas/login/"))

    def test_panel_shows_pairs_before_invite_and_add(self):
        """Lo primero que se ve son las parejas anotadas; invitar/agregar quedan abajo."""
        self.client.force_login(self.user)
        html = self.client.get("/gestion/codigo/parejas/").content.decode()
        self.assertLess(html.index("Parejas inscriptas"), html.index("Invitar pareja con código"))
        self.assertLess(html.index("Parejas inscriptas"), html.index("Agregar pareja"))
