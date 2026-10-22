"""Regresión de throttling y confianza en la IP del cliente.

No se prueba una IP real detrás de Funnel: Funnel es L4 y no entrega la IP del
visitante. Estas pruebas fijan el comportamiento seguro (no confiar en
X-Forwarded-For sin proxy declarado) y documentan la limitación multi-worker.
"""

from datetime import time, timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import CourtBooking, RegistrationRequest, Team, Tournament
from core.services import schedule as schedule_service
from core.tests_turnos import TurnosFixtureMixin
from core.views import _client_ip, _signup_attempts_key


class ClientIpTests(TestCase):
    def request(self, remote, forwarded=None):
        request = RequestFactory().get("/")
        request.META["REMOTE_ADDR"] = remote
        if forwarded is not None:
            request.META["HTTP_X_FORWARDED_FOR"] = forwarded
        return request

    @override_settings(TRUSTED_PROXY_IPS=[])
    def test_forwarded_for_is_ignored_without_a_trusted_proxy(self):
        request = self.request("10.0.0.10", "203.0.113.99, 10.0.0.10")
        self.assertEqual(_client_ip(request), "10.0.0.10")

    @override_settings(TRUSTED_PROXY_IPS=["10.0.0.0/8"])
    def test_only_the_rightmost_untrusted_address_is_used(self):
        request = self.request("10.0.0.5", "198.51.100.77, 203.0.113.9, 10.0.0.7")
        self.assertEqual(_client_ip(request), "203.0.113.9")

    @override_settings(TRUSTED_PROXY_IPS=["10.0.0.0/8"])
    def test_spoofed_left_value_does_not_win(self):
        request = self.request("10.0.0.5", "1.2.3.4, 203.0.113.9")
        self.assertEqual(_client_ip(request), "203.0.113.9")

    @override_settings(TRUSTED_PROXY_IPS=["10.0.0.0/8"])
    def test_all_trusted_chain_falls_back_to_remote_addr(self):
        request = self.request("10.0.0.5", "10.0.0.6, 10.0.0.7")
        self.assertEqual(_client_ip(request), "10.0.0.5")

    @override_settings(TRUSTED_PROXY_IPS=["10.0.0.0/8"])
    def test_garbage_forwarded_values_are_skipped(self):
        request = self.request("10.0.0.5", "not-an-ip, 203.0.113.9")
        self.assertEqual(_client_ip(request), "203.0.113.9")

    @override_settings(TRUSTED_PROXY_IPS=["no-es-una-red"])
    def test_invalid_trusted_proxy_setting_is_fail_closed(self):
        request = self.request("10.0.0.10", "203.0.113.99")
        self.assertEqual(_client_ip(request), "10.0.0.10")


class SignupCodeThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tournament = Tournament.objects.create(
            name="Torneo throttle",
            slug="torneo-throttle",
            category="primera",
            start_date=timezone.localdate(),
            status="open",
            self_registration_enabled=True,
            registration_code="CODIGO",
        )
        self.url = reverse("public_tournament_signup", args=[self.tournament.slug])
        self.payload = {
            "code": "MALO",
            "player1_first_name": "Ana",
            "player1_last_name": "Uno",
            "player1_gender": "F",
            "player1_phone": "2215550001",
            "player2_first_name": "Beto",
            "player2_last_name": "Dos",
            "player2_gender": "M",
            "player2_phone": "2215550002",
            "website": "",
        }

    def tearDown(self):
        cache.clear()

    def test_failures_are_session_keyed_and_block_the_same_session(self):
        for _ in range(15):
            self.client.post(self.url, self.payload)
        response = self.client.post(self.url, {**self.payload, "code": "CODIGO"})
        self.assertContains(response, "Demasiados intentos")
        self.assertEqual(Team.objects.count(), 0)

        other = Client()
        with mock.patch("core.views.notify_tournament_team_signup"):
            response = other.post(self.url, {**self.payload, "code": "CODIGO"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Team.objects.count(), 1)

    @override_settings(TRUSTED_PROXY_IPS=[])
    def test_shared_funnel_ip_does_not_share_the_failure_bucket_between_sessions(self):
        self.client.post(self.url, {**self.payload, "code": "MALO"})
        first_key = self.client.session.session_key
        other = Client()
        other.post(self.url, {**self.payload, "code": "MALO"})
        second_key = other.session.session_key
        self.assertNotEqual(first_key, second_key)

        request = RequestFactory().get("/")
        request.META["REMOTE_ADDR"] = "127.0.0.1"
        request.session = self.client.session
        key_one = _signup_attempts_key(request)
        request.session = other.session
        key_two = _signup_attempts_key(request)
        self.assertNotEqual(key_one, key_two)


class SignupRequestThrottleTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def payload(self, email="nuevo@correo.com", **overrides):
        data = {
            "email": email,
            "first_name": "Ana",
            "last_name": "Solicitud",
            "phone": "2215551234",
            "complex_name": "Complejo Nuevo",
            "complex_city": "La Plata",
            "complex_address": "Calle 1",
            "website": "",
        }
        data.update(overrides)
        return data

    @override_settings(SIGNUP_REQUEST_MAX_PER_WINDOW=1, SIGNUP_REQUEST_WINDOW_SECONDS=900)
    def test_signup_request_is_limited_per_session(self):
        with mock.patch("core.views.notify_new_signup_request"):
            first = self.client.post(reverse("signup_request"), self.payload())
            second = self.client.post(
                reverse("signup_request"), self.payload(email="otro@correo.com")
            )
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(RegistrationRequest.objects.count(), 1)

    @override_settings(SIGNUP_REQUEST_MAX_PER_WINDOW=1, SIGNUP_REQUEST_WINDOW_SECONDS=900)
    def test_another_session_has_its_own_bucket(self):
        with mock.patch("core.views.notify_new_signup_request"):
            self.client.post(reverse("signup_request"), self.payload())
            other = Client()
            response = other.post(
                reverse("signup_request"), self.payload(email="otro@correo.com")
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(RegistrationRequest.objects.count(), 2)

    def test_signup_request_truncates_free_text_to_the_model_limits(self):
        with mock.patch("core.views.notify_new_signup_request"):
            response = self.client.post(
                reverse("signup_request"),
                self.payload(
                    first_name="A" * 400,
                    last_name="B" * 400,
                    complex_name="C" * 400,
                    complex_city="D" * 400,
                    complex_address="E" * 400,
                ),
            )
        self.assertEqual(response.status_code, 302)
        request_row = RegistrationRequest.objects.get()
        self.assertEqual(len(request_row.first_name), 150)
        self.assertEqual(len(request_row.last_name), 150)
        self.assertEqual(len(request_row.complex_name), 120)
        self.assertEqual(len(request_row.complex_city), 120)
        self.assertEqual(len(request_row.complex_address), 200)


class ReservationQuotaTests(TurnosFixtureMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.complex = self.make_complex()
        self.court = self.make_court(self.complex)
        self.day = timezone.localdate() + timedelta(days=5)
        self.make_schedule(
            self.complex, day_type=schedule_service.day_type_for(self.day)
        )

    def reserve(self, hora, phone="2215551234", client=None):
        data = {
            "complejo": self.complex.slug,
            "fecha": self.day.isoformat(),
            "cancha": self.court.pk,
            "hora": hora,
            "nombre": "Ana Cuota",
            "telefono": phone,
            "nota": "",
        }
        return (client or self.client).post(reverse("turno_reservar"), data)

    @override_settings(PUBLIC_RESERVATION_MAX_ACTIVE=2, PUBLIC_RESERVATION_MAX_PER_HOUR=10)
    def test_active_reservation_quota_is_per_phone_and_complex(self):
        self.reserve("09:00")
        self.reserve("10:30")
        response = self.reserve("12:00")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            CourtBooking.objects.filter(customer_phone="2215551234").count(), 2
        )

        other_complex = self.make_complex(slug="complejo-cuota", name="Complejo cuota")
        other_court = self.make_court(other_complex)
        self.make_schedule(
            other_complex, day_type=schedule_service.day_type_for(self.day)
        )
        response = self.client.post(
            reverse("turno_reservar"),
            {
                "complejo": other_complex.slug,
                "fecha": self.day.isoformat(),
                "cancha": other_court.pk,
                "hora": "09:00",
                "nombre": "Ana Cuota",
                "telefono": "2215551234",
                "nota": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CourtBooking.objects.filter(customer_phone="2215551234").count(), 3)

    @override_settings(PUBLIC_RESERVATION_MAX_ACTIVE=10, PUBLIC_RESERVATION_MAX_PER_HOUR=2)
    def test_hourly_quota_blocks_bursts_even_with_free_slots(self):
        self.reserve("09:00")
        self.reserve("10:30")
        self.reserve("12:00")
        self.assertEqual(CourtBooking.objects.count(), 2)

    @override_settings(PUBLIC_RESERVATION_MAX_ACTIVE=1, PUBLIC_RESERVATION_MAX_PER_HOUR=10)
    def test_cancelling_frees_the_active_quota(self):
        self.reserve("09:00")
        booking = CourtBooking.objects.get()
        self.client.post(reverse("turno_cancelar", args=[booking.code]))
        self.reserve("09:00")
        self.assertEqual(CourtBooking.objects.filter(status="booked").count(), 1)

    @override_settings(PUBLIC_RESERVATION_MAX_ACTIVE=1, PUBLIC_RESERVATION_MAX_PER_HOUR=10)
    def test_normalized_phone_prevents_formatting_bypass(self):
        self.reserve("09:00", phone="221-555-1234")
        self.reserve("10:30", phone="221 555 1234")
        self.assertEqual(CourtBooking.objects.count(), 1)


class PublicCodeThrottleTests(TurnosFixtureMixin, TestCase):
    """El código de turno es una credencial bearer: limitar su consulta."""

    def setUp(self):
        cache.clear()
        self.complex = self.make_complex()
        self.court = self.make_court(self.complex)
        self.day = timezone.localdate() + timedelta(days=5)
        self.make_schedule(
            self.complex, day_type=schedule_service.day_type_for(self.day)
        )
        self.booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(9, 0),
            status="booked",
            customer_name="Titular",
        )

    def tearDown(self):
        cache.clear()

    @override_settings(PUBLIC_CODE_MAX_PER_WINDOW=2, PUBLIC_CODE_WINDOW_SECONDS=300)
    def test_confirmation_code_is_limited_per_session(self):
        url = reverse("turno_confirmado", args=[self.booking.code])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Demasiadas consultas", status_code=429)

        # Otra sesión conserva su propio cupo.
        self.assertEqual(Client().get(url).status_code, 200)

        # El límite es por endpoint: cancelar no queda bloqueado por confirmar.
        cancel_url = reverse("turno_cancelar", args=[self.booking.code])
        self.assertEqual(self.client.get(cancel_url).status_code, 200)

    @override_settings(PUBLIC_CODE_MAX_PER_WINDOW=2, PUBLIC_CODE_WINDOW_SECONDS=300)
    def test_cancel_get_and_post_share_the_limit_without_mutating(self):
        url = reverse("turno_cancelar", args=[self.booking.code])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 429)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "booked")

    @override_settings(PUBLIC_CODE_MAX_PER_WINDOW=2, PUBLIC_CODE_WINDOW_SECONDS=300)
    def test_pay_and_return_scopes_are_limited_independently(self):
        pay_url = reverse("turno_pagar", args=[self.booking.code])
        for _ in range(2):
            self.assertEqual(self.client.get(pay_url).status_code, 200)
        self.assertEqual(self.client.get(pay_url).status_code, 429)

        return_url = reverse("turno_pago_retorno", args=[self.booking.code])
        for _ in range(2):
            self.assertEqual(self.client.get(return_url).status_code, 200)
        self.assertEqual(self.client.get(return_url).status_code, 429)

    def test_unknown_code_returns_404_without_disclosing_data(self):
        response = self.client.get(
            reverse("turno_confirmado", args=["ZZZZZZZZ"])
        )
        self.assertEqual(response.status_code, 404)

    @override_settings(PUBLIC_CODE_MAX_PER_WINDOW=0)
    def test_zero_disables_the_limit(self):
        url = reverse("turno_confirmado", args=[self.booking.code])
        for _ in range(5):
            self.assertEqual(self.client.get(url).status_code, 200)
