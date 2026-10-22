"""Objetivo 4: concurrencia de partidos contra PostgreSQL real.

Estas pruebas usan ``TransactionTestCase`` y conexiones independientes porque
los locks de fila no se pueden probar dentro de la transacción única de
``TestCase``. En SQLite se saltean.
"""

import hashlib
import hmac
import json
import threading
import unittest
from datetime import time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection, connections, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import CourtPayment, Match, Tournament, Zone, ZoneTeam
from core.services import draws
from core.services import schedule as schedule_service
from core.services.matches import create_group_match, delete_group_match
from core.services.tournament_workflow import (
    MatchConflictError,
    register_match_result,
)
from core.tests import TournamentFixtureMixin
from core.tests_turnos import TurnosFixtureMixin

PG_ONLY = unittest.skipUnless(
    connection.vendor == "postgresql",
    "Los locks de fila requieren PostgreSQL.",
)


@PG_ONLY
class MatchConcurrencyTests(TournamentFixtureMixin, TransactionTestCase):
    def setUp(self):
        self.tournament = self.make_tournament(
            slug="concurrencia", format_type="solo_zonas"
        )
        self.teams = self.add_teams(self.tournament, 2)
        self.zone = Zone.objects.create(
            tournament=self.tournament, name="A", order=1
        )
        for team in self.teams:
            ZoneTeam.objects.create(zone=self.zone, team=team)
        self.match = self._ensure_match(self.zone, self.teams[0], self.teams[1])

    def _ensure_match(self, zone, team_a, team_b):
        return create_group_match(
            self.tournament, zone, team_a, team_b
        )

    def _run_threads(self, targets):
        barrier = threading.Barrier(len(targets))
        results = [None] * len(targets)
        errors = [None] * len(targets)

        def wrap(index, target):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                results[index] = target()
            except Exception as exc:  # noqa: BLE001 - se reporta en el assert
                errors[index] = exc
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=wrap, args=(index, target), daemon=True)
            for index, target in enumerate(targets)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        return results, errors

    def test_two_incompatible_results_on_the_same_match(self):
        team1, team2 = self.teams
        scores = ["6-4 6-3", "4-6 3-6"]

        def play(index):
            def target():
                match = Match.objects.get(pk=self.match.pk)
                register_match_result(
                    match,
                    score=scores[index],
                    winner=team1 if index == 0 else team2,
                    expected_version=0,
                )
                return index

            return target

        results, errors = self._run_threads([play(0), play(1)])
        successes = [value for value in results if value is not None]
        failures = [error for error in errors if error is not None]
        self.assertEqual(len(successes), 1, (results, errors))
        self.assertEqual(len(failures), 1, (results, errors))
        self.assertIsInstance(failures[0], MatchConflictError)

        final = Match.objects.get(pk=self.match.pk)
        self.assertEqual(final.version, 1)
        self.assertIn(final.score, scores)
        expected_winner = team1.pk if final.score == scores[0] else team2.pk
        self.assertEqual(final.winner_id, expected_winner)

    def test_two_matches_of_the_same_tournament_are_serialized_without_deadlock(self):
        third = self.add_team(self.tournament)
        ZoneTeam.objects.create(zone=self.zone, team=third)
        second = create_group_match(self.tournament, self.zone, self.teams[0], third)
        first = self.match

        def play(match, team):
            def target():
                current = Match.objects.get(pk=match.pk)
                register_match_result(
                    current,
                    score="6-4 6-3",
                    winner=team,
                    expected_version=0,
                )
                return match.pk

            return target

        results, errors = self._run_threads(
            [play(first, self.teams[0]), play(second, self.teams[0])]
        )
        self.assertEqual(errors, [None, None])
        self.assertEqual(
            Match.objects.filter(pk__in=[first.pk, second.pk], winner__isnull=False).count(),
            2,
        )

    def test_different_tournaments_do_not_block_each_other(self):
        other = self.make_tournament(slug="concurrencia-2", format_type="solo_zonas")
        locked = threading.Event()
        release = threading.Event()
        acquired = threading.Event()

        def hold_first():
            connections.close_all()
            try:
                with transaction.atomic():
                    Tournament.objects.select_for_update().get(pk=self.tournament.pk)
                    locked.set()
                    release.wait(timeout=10)
            finally:
                connections.close_all()

        def lock_second():
            connections.close_all()
            try:
                with transaction.atomic():
                    Tournament.objects.select_for_update().get(pk=other.pk)
                    acquired.set()
            finally:
                connections.close_all()

        holder = threading.Thread(target=hold_first, daemon=True)
        holder.start()
        self.assertTrue(locked.wait(timeout=10))
        contender = threading.Thread(target=lock_second, daemon=True)
        contender.start()
        try:
            self.assertTrue(
                acquired.wait(timeout=3),
                "El lock de un torneo bloqueó innecesariamente a otro torneo.",
            )
        finally:
            release.set()
            holder.join(timeout=10)
            contender.join(timeout=10)

    def test_concurrent_bracket_generation_leaves_a_single_structure(self):
        tournament = self.make_tournament(
            slug="llave-concurrente", format_type="zonas_playoffs", teams_per_zone=2
        )
        teams = self.add_teams(tournament, 2)
        zone = Zone.objects.create(tournament=tournament, name="A", order=1)
        for team in teams:
            ZoneTeam.objects.create(zone=zone, team=team)
        match = create_group_match(tournament, zone, teams[0], teams[1])
        register_match_result(
            Match.objects.get(pk=match.pk),
            score="6-4 6-3",
            winner=teams[0],
            expected_version=0,
        )

        def generate():
            draws.generate_playoff_bracket(tournament)

        _results, errors = self._run_threads([generate, generate])
        self.assertEqual(errors, [None, None])
        playoff_matches = Match.objects.filter(
            tournament=tournament, stage__in=draws.PLAYOFF_STAGES
        )
        self.assertEqual(playoff_matches.count(), 1)
        self.assertEqual(playoff_matches.filter(stage="final").count(), 1)

    def test_concurrent_duplicate_group_match_is_rejected(self):
        tournament = self.make_tournament(slug="alta-duplicada", format_type="solo_zonas")
        teams = self.add_teams(tournament, 2)
        zone = Zone.objects.create(tournament=tournament, name="B", order=1)
        for team in teams:
            ZoneTeam.objects.create(zone=zone, team=team)

        def create():
            create_group_match(
                tournament, zone, teams[0], teams[1]
            )

        _results, errors = self._run_threads([create, create])
        self.assertEqual(len([error for error in errors if error is None]), 1)
        self.assertEqual(len([error for error in errors if error is not None]), 1)
        self.assertEqual(
            Match.objects.filter(tournament=tournament, zone=zone).count(), 1
        )

    def test_delete_and_result_do_not_lose_the_result_silently(self):
        match = self.match
        team = self.teams[0]

        def save_result():
            current = Match.objects.get(pk=match.pk)
            register_match_result(
                current,
                score="6-4 6-3",
                winner=team,
                expected_version=0,
            )
            return "result"

        def remove():
            delete_group_match(Match.objects.get(pk=match.pk))
            return "deleted"

        results, errors = self._run_threads([save_result, remove])
        succeeded = [value for value in results if value]
        # Exactamente una operación gana; la otra termina con un error de
        # validación, nunca con un 500 ni con un resultado pisado.
        self.assertEqual(len(succeeded), 1, (results, errors))
        self.assertEqual(len([error for error in errors if error is not None]), 1)
        exists = Match.objects.filter(pk=match.pk).exists()
        if exists:
            final = Match.objects.get(pk=match.pk)
            self.assertEqual(final.score, "6-4 6-3")
            self.assertEqual(final.winner_id, team.pk)
        else:
            self.assertIn("deleted", succeeded)

    def test_stale_match_instance_cannot_overwrite_a_newer_save(self):
        first = Match.objects.get(pk=self.match.pk)
        stale = Match.objects.get(pk=self.match.pk)
        first.score = "6-4 6-3"
        first.winner = self.teams[0]
        first.save()

        stale.score = "4-6 3-6"
        stale.winner = self.teams[1]
        with self.assertRaises(MatchConflictError):
            stale.save()

        final = Match.objects.get(pk=self.match.pk)
        self.assertEqual(final.score, "6-4 6-3")
        self.assertEqual(final.winner_id, self.teams[0].pk)
        self.assertEqual(final.version, 1)

    def test_deleted_match_returns_a_controlled_conflict(self):
        stale = Match.objects.get(pk=self.match.pk)
        Match.objects.filter(pk=self.match.pk).delete()
        with self.assertRaises(MatchConflictError):
            register_match_result(
                stale,
                score="6-4 6-3",
                winner=self.teams[0],
                expected_version=0,
            )

    def test_bracket_sync_increments_the_target_version(self):
        tournament = self.make_tournament(
            slug="version-llave",
            format_type="zonas_playoffs",
            teams_per_zone=2,
            qualifiers_per_zone=2,
        )
        self.add_teams(tournament, 4)
        zones = self.make_zones(tournament, [2, 2])
        # Completar ambas zonas genera la llave de 4 (semifinales + final).
        for zone in zones:
            match = zone.matches.get(stage="group")
            self.play_match(match, match.team1)

        semi = Match.objects.filter(tournament=tournament, stage="semi").order_by("slot").first()
        self.assertIsNotNone(semi)
        winner = semi.team1
        register_match_result(
            Match.objects.get(pk=semi.pk),
            score="6-4 6-3",
            winner=winner,
            expected_version=semi.version,
        )
        final = Match.objects.get(tournament=tournament, stage="final")
        self.assertEqual(final.team1_id, winner.pk)
        self.assertGreaterEqual(final.version, 1)


@PG_ONLY
@override_settings(MERCADO_PAGO_ONLINE_PAYMENTS_ENABLED=True)
class CourtCancellationConcurrencyTests(TurnosFixtureMixin, TransactionTestCase):
    """La cancelación del panel y el webhook deben serializarse por turno."""

    def setUp(self):
        self.complex = self.make_complex(booking_price=Decimal("8000"))
        self.complex.mp_access_token = "APP_USR-12345678901234567890"
        self.complex.mp_webhook_secret = "secreto-webhook-1234567890"
        self.complex.save(
            update_fields=[
                "mp_access_token_encrypted",
                "mp_webhook_secret_encrypted",
            ]
        )
        self.court = self.make_court(self.complex)
        self.day = timezone.localdate() + timedelta(days=5)
        self.make_schedule(
            self.complex, day_type=schedule_service.day_type_for(self.day)
        )
        self.admin = get_user_model().objects.create_superuser(
            "admin-carrera", password="clave-segura-123"
        )
        self.booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(9, 0),
            status="pending_payment",
            customer_name="Carrera panel webhook",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
            payment_expires_at=timezone.now() + timedelta(minutes=15),
        )

    def _signature(self, payment_id, request_id):
        timestamp = "1704908010"
        manifest = f"id:{payment_id};request-id:{request_id};ts:{timestamp};"
        digest = hmac.new(
            self.complex.mp_webhook_secret.encode("utf-8"),
            manifest.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"ts={timestamp},v1={digest}"

    def test_panel_cancel_and_webhook_do_not_lose_or_duplicate_the_payment(self):
        payment_id = "race-payment-1"
        request_id = "race-request-1"
        payment = {
            "id": payment_id,
            "status": "approved",
            "external_reference": self.booking.code,
            "transaction_amount": "2400.00",
            "currency_id": "ARS",
        }
        barrier = threading.Barrier(2)
        results = {}
        errors = {}

        def run(name, target):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                results[name] = target()
            except Exception as exc:  # noqa: BLE001 - se reporta en el assert
                errors[name] = exc
            finally:
                connections.close_all()

        def cancel_from_panel():
            client = Client()
            client.force_login(self.admin)
            return client.post(
                reverse("panel_turno_accion"),
                {
                    "complejo": self.complex.slug,
                    "fecha": self.day.isoformat(),
                    "accion": "cancelar",
                    "turno_id": self.booking.pk,
                },
            )

        def notify_webhook():
            client = Client()
            return client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps(
                    {"type": "payment", "data": {"id": payment_id}}
                ),
                content_type="application/json",
                HTTP_X_REQUEST_ID=request_id,
                HTTP_X_SIGNATURE=self._signature(payment_id, request_id),
            )

        with mock.patch(
            "core.views.payments_service.fetch_payment", return_value=payment
        ), mock.patch("core.views.notify_court_booking"):
            threads = [
                threading.Thread(
                    target=run, args=("panel", cancel_from_panel), daemon=True
                ),
                threading.Thread(
                    target=run, args=("webhook", notify_webhook), daemon=True
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertEqual(errors, {})
        self.assertEqual(results["panel"].status_code, 302)
        self.assertEqual(results["webhook"].status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "cancelled")
        self.assertTrue(self.booking.is_paid)
        self.assertIn(
            self.booking.payment_status, ("approved", "approved_conflict")
        )
        self.assertEqual(
            CourtPayment.objects.filter(
                booking=self.booking, kind="deposit"
            ).count(),
            1,
        )
