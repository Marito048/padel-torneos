"""Pruebas del turnero de canchas.

Cubre la grilla de horarios (duración, pausas, feriados y fechas especiales),
la reserva pública sin registro (alta, conflictos y cancelación) y la gestión
desde el panel (reservas manuales, bloqueos y configuración de horarios).
"""

import hashlib
import hmac
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    CashClose,
    Complex,
    Court,
    CourtBooking,
    CourtDateOverride,
    CourtPayment,
    CourtSchedule,
    SchedulePause,
)
from core.services import cash as cash_service
from core.services import payments as payments_service
from core.services import schedule as schedule_service

User = get_user_model()

# Fechas fijas: 2030-01-07 es lunes, 2030-01-12 sábado y 2030-01-13 domingo.
MONDAY = date(2030, 1, 7)
SATURDAY = date(2030, 1, 12)
SUNDAY = date(2030, 1, 13)


class TurnosFixtureMixin:
    def make_complex(self, slug="complejo-1", name="Complejo 1", **kwargs):
        return Complex.objects.create(name=name, slug=slug, **kwargs)

    def make_court(self, complex_obj, name="Cancha 1", **kwargs):
        return Court.objects.create(complex=complex_obj, name=name, **kwargs)

    def make_schedule(self, complex_obj, day_type="weekday", **kwargs):
        return CourtSchedule.objects.create(complex=complex_obj, day_type=day_type, **kwargs)

    @staticmethod
    def add_minutes(start, minutes):
        return (datetime.combine(date(2000, 1, 1), start) + timedelta(minutes=minutes)).time()

    def make_booking(self, complex_obj, court, day, start, status="booked", **kwargs):
        return CourtBooking.objects.create(
            complex=complex_obj,
            court=court,
            date=day,
            start_time=start,
            end_time=self.add_minutes(start, 90),
            status=status,
            **kwargs,
        )


class SlotGenerationTests(TurnosFixtureMixin, TestCase):
    def setUp(self):
        self.complex = self.make_complex()

    def slot_starts(self, day):
        return [start.strftime("%H:%M") for start, _ in schedule_service.slots_for_day(self.complex, day)]

    def test_default_weekday_grid(self):
        # 09:00 a 23:00 con turnos de 90 minutos → 9 turnos.
        self.assertEqual(
            self.slot_starts(MONDAY),
            ["09:00", "10:30", "12:00", "13:30", "15:00", "16:30", "18:00", "19:30", "21:00"],
        )

    def test_pause_breaks_the_sequence_and_resumes_after(self):
        schedule = self.make_schedule(self.complex)
        SchedulePause.objects.create(schedule=schedule, start_time="13:00", end_time="14:00")
        # El turno de 12:00 se pisa con la pausa y el siguiente arranca 14:00.
        self.assertEqual(
            self.slot_starts(MONDAY),
            ["09:00", "10:30", "14:00", "15:30", "17:00", "18:30", "20:00", "21:30"],
        )

    def test_half_hour_pause(self):
        schedule = self.make_schedule(self.complex)
        SchedulePause.objects.create(schedule=schedule, start_time="13:30", end_time="14:00")
        self.assertEqual(
            self.slot_starts(MONDAY),
            ["09:00", "10:30", "12:00", "14:00", "15:30", "17:00", "18:30", "20:00", "21:30"],
        )

    def test_each_day_type_has_its_own_start(self):
        self.make_schedule(self.complex, day_type="weekday", start_time="09:00", end_time="23:00")
        self.make_schedule(self.complex, day_type="saturday", start_time="10:00", end_time="22:00", slot_minutes=120)
        self.make_schedule(self.complex, day_type="sunday", start_time="15:00", end_time="21:00")
        self.assertEqual(
            self.slot_starts(SATURDAY),
            ["10:00", "12:00", "14:00", "16:00", "18:00", "20:00"],
        )
        self.assertEqual(self.slot_starts(SUNDAY), ["15:00", "16:30", "18:00", "19:30"])

    def test_closed_day_type_has_no_slots(self):
        self.make_schedule(self.complex, day_type="sunday", is_open=False)
        self.assertEqual(self.slot_starts(SUNDAY), [])
        self.assertEqual(len(self.slot_starts(MONDAY)), 9)

    def test_closed_override_cancels_everything(self):
        CourtDateOverride.objects.create(complex=self.complex, date=MONDAY, is_closed=True, note="feriado")
        self.assertEqual(self.slot_starts(MONDAY), [])
        config = schedule_service.day_config(self.complex, MONDAY)
        self.assertFalse(config.is_open)
        self.assertEqual(config.override.note, "feriado")

    def test_open_override_sets_custom_hours(self):
        CourtDateOverride.objects.create(
            complex=self.complex, date=MONDAY, is_closed=False, start_time="15:00", end_time="21:00"
        )
        self.assertEqual(self.slot_starts(MONDAY), ["15:00", "16:30", "18:00", "19:30"])

    def test_override_hours_multiple_of_slot(self):
        # Cierre 20:15 no coincide con la grilla: el último turno empieza 18:00 y termina 19:30.
        self.make_schedule(self.complex, slot_minutes=90)
        CourtDateOverride.objects.create(
            complex=self.complex, date=MONDAY, is_closed=False, start_time="18:00", end_time="20:15"
        )
        self.assertEqual(self.slot_starts(MONDAY), ["18:00"])

    def test_slot_for_validates_grid(self):
        self.assertIsNotNone(schedule_service.slot_for(self.complex, MONDAY, time(9, 0)))
        self.assertIsNone(schedule_service.slot_for(self.complex, MONDAY, time(9, 10)))

    def test_is_slot_past(self):
        today = timezone.localdate()
        self.assertTrue(schedule_service.is_slot_past(today - timedelta(days=1), time(9, 0)))
        self.assertFalse(schedule_service.is_slot_past(today + timedelta(days=1), time(9, 0)))

    def test_unique_active_booking_constraint(self):
        court = self.make_court(self.complex)
        self.make_booking(self.complex, court, MONDAY, time(9, 0))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_booking(self.complex, court, MONDAY, time(9, 0))

    def test_cancelled_booking_frees_the_slot(self):
        court = self.make_court(self.complex)
        booking = self.make_booking(self.complex, court, MONDAY, time(9, 0))
        booking.status = "cancelled"
        booking.save(update_fields=["status"])
        # No hay choque: el mismo horario se puede volver a reservar.
        self.make_booking(self.complex, court, MONDAY, time(9, 0))


class PublicBookingTests(TurnosFixtureMixin, TestCase):
    def setUp(self):
        self.complex = self.make_complex()
        self.court = self.make_court(self.complex)
        self.day = timezone.localdate() + timedelta(days=5)
        # Asegura que el día elegido tenga la grilla hábil aunque caiga fin de semana.
        self.make_schedule(self.complex, day_type=schedule_service.day_type_for(self.day))

    def reserve(self, **overrides):
        data = {
            "complejo": self.complex.slug,
            "fecha": self.day.isoformat(),
            "cancha": self.court.pk,
            "hora": "09:00",
            "nombre": "Ana Pérez",
            "telefono": "2215551234",
            "nota": "",
        }
        data.update(overrides)
        return self.client.post(reverse("turno_reservar"), data)

    def test_public_page_shows_free_slots(self):
        response = self.client.get(reverse("turnos"), {"complejo": self.complex.slug, "fecha": self.day.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Libre")
        self.assertContains(response, self.complex.name)

    def test_booking_form_prefilled_for_free_slot(self):
        response = self.client.get(
            reverse("turnos"),
            {"complejo": self.complex.slug, "fecha": self.day.isoformat(), "cancha": self.court.pk, "hora": "09:00"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirmá tu turno")

    def test_public_booking_creates_and_redirects_to_confirmation(self):
        response = self.reserve()
        self.assertEqual(response.status_code, 302)
        booking = CourtBooking.objects.get()
        self.assertEqual(booking.status, "booked")
        self.assertEqual(booking.source, "public")
        self.assertEqual(booking.customer_name, "Ana Pérez")
        self.assertEqual(len(booking.code), 8)
        self.assertRedirects(response, reverse("turno_confirmado", args=[booking.code]))

    def test_complex_without_mercadopago_keeps_the_manual_flow(self):
        self.assertFalse(self.complex.has_mp_token)
        self.assertFalse(self.complex.online_payments_enabled)
        response = self.reserve()
        self.assertEqual(response.status_code, 302)
        booking = CourtBooking.objects.get()
        self.assertEqual(booking.status, "booked")
        self.assertIsNone(booking.payment_expires_at)
        self.assertEqual(booking.payment_status, "")
        self.assertFalse(booking.payment_provider)

    def test_booked_slot_cannot_be_reserved_again(self):
        self.reserve()
        response = self.reserve(nombre="Otro Cliente")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CourtBooking.objects.count(), 1)
        # La grilla pública muestra el horario como ocupado.
        self.assertContains(self.client.get(response.url), "Ocupado")

    def test_booking_requires_name_and_phone(self):
        self.reserve(nombre="")
        self.reserve(telefono="123")
        self.assertEqual(CourtBooking.objects.count(), 0)

    def test_booking_outside_grid_rejected(self):
        self.reserve(hora="09:10")
        self.assertEqual(CourtBooking.objects.count(), 0)

    def test_past_date_rejected(self):
        self.reserve(fecha=(timezone.localdate() - timedelta(days=1)).isoformat())
        self.assertEqual(CourtBooking.objects.count(), 0)

    def test_cancellation_frees_the_slot(self):
        self.reserve()
        booking = CourtBooking.objects.get()
        response = self.client.post(reverse("turno_cancelar", args=[booking.code]))
        self.assertRedirects(response, reverse("turno_confirmado", args=[booking.code]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        self.assertIsNotNone(booking.cancelled_at)
        # El horario vuelve a estar libre.
        response = self.reserve(nombre="Otra Persona")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CourtBooking.objects.filter(status="booked").count(), 1)

    def test_confirmation_page_shows_code(self):
        self.reserve()
        booking = CourtBooking.objects.get()
        response = self.client.get(reverse("turno_confirmado", args=[booking.code]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, booking.code)

    def test_complex_without_courts_is_not_offered_publicly(self):
        other = self.make_complex(slug="complejo-2", name="Complejo 2")
        response = self.client.get(reverse("turnos"), {"complejo": other.slug, "fecha": self.day.isoformat()})
        self.assertEqual(response.status_code, 200)
        # El selector público solo muestra complejos con canchas activas.
        self.assertNotContains(response, "Complejo 2")
        self.assertContains(response, self.complex.name)

    def test_no_bookable_complexes_shows_message(self):
        Court.objects.update(is_active=False)
        response = self.client.get(reverse("turnos"), {"fecha": self.day.isoformat()})
        self.assertContains(response, "no hay complejos disponibles")


class PanelTurnosTests(TurnosFixtureMixin, TestCase):
    def setUp(self):
        self.complex = self.make_complex()
        self.court = self.make_court(self.complex)
        self.other_complex = self.make_complex(slug="complejo-2", name="Complejo 2")
        self.day = timezone.localdate() + timedelta(days=5)
        self.make_schedule(self.complex, day_type=schedule_service.day_type_for(self.day))

        self.admin = User.objects.create_superuser("admin-turnos", password="clave-segura-123")
        self.staff = User.objects.create_user("staff-turnos", password="clave-segura-123")
        self.staff.complexes.add(self.other_complex)

    def panel_data(self, action="reservar", **overrides):
        data = {
            "complejo": self.complex.slug,
            "fecha": self.day.isoformat(),
            "accion": action,
            "cancha": self.court.pk,
            "hora": "09:00",
            "nombre": "Cliente Panel",
            "telefono": "2215550000",
            "nota": "",
        }
        data.update(overrides)
        return data

    def test_panel_requires_login(self):
        response = self.client.get(reverse("panel_turnos"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response.url)

    def test_staff_of_other_complex_cannot_book(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse("panel_turno_accion"), self.panel_data())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(CourtBooking.objects.count(), 0)

    def test_admin_creates_manual_booking(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("panel_turno_accion"), self.panel_data())
        self.assertEqual(response.status_code, 302)
        booking = CourtBooking.objects.get()
        self.assertEqual(booking.source, "panel")
        self.assertEqual(booking.created_by, self.admin)
        self.assertEqual(booking.customer_name, "Cliente Panel")

    def test_block_and_release_slot(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("panel_turno_accion"), self.panel_data(action="bloquear", nombre=""))
        self.assertEqual(response.status_code, 302)
        booking = CourtBooking.objects.get()
        self.assertEqual(booking.status, "blocked")

        response = self.client.post(
            reverse("panel_turno_accion"),
            {
                "complejo": self.complex.slug,
                "fecha": self.day.isoformat(),
                "accion": "cancelar",
                "turno_id": booking.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")

    def test_schedule_saved_from_panel(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("panel_horarios_accion"),
            {
                "complejo": self.complex.slug,
                "accion": "guardar_horario",
                "tipo_dia": "saturday",
                "abierto": "on",
                "desde": "10:00",
                "hasta": "22:00",
                "duracion": "120",
            },
        )
        self.assertEqual(response.status_code, 302)
        schedule = CourtSchedule.objects.get(complex=self.complex, day_type="saturday")
        self.assertEqual(schedule.start_time.strftime("%H:%M"), "10:00")
        self.assertEqual(schedule.slot_minutes, 120)

    def test_pause_added_from_panel(self):
        self.client.force_login(self.admin)
        schedule, _ = CourtSchedule.objects.get_or_create(
            complex=self.complex, day_type="weekday"
        )
        response = self.client.post(
            reverse("panel_horarios_accion"),
            {
                "complejo": self.complex.slug,
                "accion": "agregar_pausa",
                "horario_id": schedule.pk,
                "desde": "13:00",
                "hasta": "14:00",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(schedule.pauses.count(), 1)

    def test_special_date_from_panel(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("panel_horarios_accion"),
            {
                "complejo": self.complex.slug,
                "accion": "guardar_fecha",
                "fecha": self.day.isoformat(),
                "cerrado": "on",
                "nota": "feriado",
            },
        )
        self.assertEqual(response.status_code, 302)
        override = CourtDateOverride.objects.get(complex=self.complex, date=self.day)
        self.assertTrue(override.is_closed)

    def test_grid_shows_customer_name_in_panel(self):
        self.client.force_login(self.admin)
        self.make_booking(self.complex, self.court, self.day, time(9, 0), customer_name="Ana")
        response = self.client.get(
            reverse("panel_turnos"), {"complejo": self.complex.slug, "fecha": self.day.isoformat()}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ana")

    def test_selected_booking_opens_its_detail_panel(self):
        self.client.force_login(self.admin)
        booking = self.make_booking(
            self.complex, self.court, self.day, time(9, 0), customer_name="Ana"
        )
        response = self.client.get(
            reverse("panel_turnos"),
            {
                "complejo": self.complex.slug,
                "fecha": self.day.isoformat(),
                "turno": booking.pk,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'id="turno-detalle-{booking.pk}"')
        self.assertContains(response, "Detalle del turno")
        self.assertContains(response, "Cancelar turno")
        self.assertContains(response, f'data-selected-turno="{booking.pk}"')

    def test_selected_booking_from_another_complex_is_ignored(self):
        self.client.force_login(self.admin)
        other_court = self.make_court(self.other_complex)
        other_booking = self.make_booking(
            self.other_complex, other_court, self.day, time(9, 0), customer_name="Otra complejo"
        )
        response = self.client.get(
            reverse("panel_turnos"),
            {
                "complejo": self.complex.slug,
                "fecha": self.day.isoformat(),
                "turno": other_booking.pk,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'id="turno-detalle-{other_booking.pk}"')
        self.assertContains(response, 'data-selected-turno=""')

    def test_blocked_booking_does_not_open_a_detail_panel(self):
        self.client.force_login(self.admin)
        booking = self.make_booking(
            self.complex, self.court, self.day, time(9, 0), status="blocked", note="Mantenimiento"
        )
        response = self.client.get(
            reverse("panel_turnos"),
            {
                "complejo": self.complex.slug,
                "fecha": self.day.isoformat(),
                "turno": booking.pk,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'id="turno-detalle-{booking.pk}"')
        self.assertContains(response, 'data-selected-turno=""')
        self.assertContains(response, "Mantenimiento")

    def test_cancelled_bookings_are_in_a_collapsible_history(self):
        self.client.force_login(self.admin)
        self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(9, 0),
            status="cancelled",
            customer_name="Cliente cancelado",
        )
        response = self.client.get(
            reverse("panel_turnos"), {"complejo": self.complex.slug, "fecha": self.day.isoformat()}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Reservas del día")
        self.assertContains(response, "Turnos cancelados del día (1)")
        self.assertContains(response, "Cliente cancelado")


class MonthCalendarTests(TurnosFixtureMixin, TestCase):
    def setUp(self):
        self.complex = self.make_complex()

    def cells_for(self, month_start, today, last_day, selected=None):
        weeks = schedule_service.month_calendar(
            self.complex, month_start, today=today, last_day=last_day, selected=selected
        )
        return {cell["date"]: cell for week in weeks for cell in week}

    def test_weeks_start_on_monday_and_mark_out_of_month_days(self):
        weeks = schedule_service.month_calendar(
            self.complex, date(2030, 1, 1), today=date(2030, 1, 1), last_day=date(2030, 3, 31)
        )
        # 2030-01-01 es martes: la primera semana arranca el lunes 31/12/2029.
        self.assertEqual(weeks[0][0]["date"], date(2029, 12, 31))
        self.assertFalse(weeks[0][0]["in_month"])
        self.assertEqual(weeks[0][1]["date"], date(2030, 1, 1))
        self.assertTrue(weeks[0][1]["in_month"])
        # La última semana cierra el domingo 03/02/2030.
        self.assertEqual(weeks[-1][-1]["date"], date(2030, 2, 3))

    def test_selectable_only_inside_booking_window(self):
        today = date(2030, 1, 7)
        last_day = date(2030, 1, 20)
        cells = self.cells_for(date(2030, 1, 1), today, last_day)
        self.assertFalse(cells[date(2030, 1, 6)]["selectable"])
        self.assertTrue(cells[date(2030, 1, 7)]["selectable"])
        self.assertTrue(cells[date(2030, 1, 20)]["selectable"])
        self.assertFalse(cells[date(2030, 1, 21)]["selectable"])

    def test_closed_override_and_closed_day_type_mark_cells(self):
        self.make_schedule(self.complex, day_type="sunday", is_open=False)
        CourtDateOverride.objects.create(complex=self.complex, date=MONDAY, is_closed=True)
        cells = self.cells_for(date(2030, 1, 1), date(2030, 1, 1), date(2030, 1, 31))
        self.assertFalse(cells[date(2030, 1, 7)]["is_open"])  # lunes cerrado por fecha especial
        self.assertTrue(cells[date(2030, 1, 8)]["is_open"])  # martes normal
        self.assertFalse(cells[date(2030, 1, 13)]["is_open"])  # domingo cerrado por configuración

    def test_today_and_selected_flags(self):
        cells = self.cells_for(date(2030, 1, 1), date(2030, 1, 7), date(2030, 1, 31), selected=date(2030, 1, 9))
        self.assertTrue(cells[date(2030, 1, 7)]["is_today"])
        self.assertTrue(cells[date(2030, 1, 9)]["is_selected"])
        self.assertFalse(cells[date(2030, 1, 8)]["is_selected"])


class PaymentAndWhatsAppTests(TurnosFixtureMixin, TestCase):
    """Precio del turno, datos de Mercado Pago y avisos por WhatsApp."""

    def setUp(self):
        self.complex = self.make_complex()
        self.court = self.make_court(self.complex)
        self.day = timezone.localdate() + timedelta(days=5)
        self.make_schedule(self.complex, day_type=schedule_service.day_type_for(self.day))

    def reserve(self, **overrides):
        data = {
            "complejo": self.complex.slug,
            "fecha": self.day.isoformat(),
            "cancha": self.court.pk,
            "hora": "09:00",
            "nombre": "Ana Pérez",
            "telefono": "2215551234",
            "nota": "",
        }
        data.update(overrides)
        return self.client.post(reverse("turno_reservar"), data)

    def test_public_booking_snapshots_the_price_and_shows_payment_data(self):
        self.complex.booking_price = Decimal("7500")
        self.complex.mp_alias = "padel.principal"
        self.complex.mp_holder = "Juan Pérez"
        self.complex.save(update_fields=["booking_price", "mp_alias", "mp_holder"])
        self.reserve()
        booking = CourtBooking.objects.get()
        self.assertEqual(booking.price, Decimal("7500.00"))
        response = self.client.get(reverse("turno_confirmado", args=[booking.code]))
        self.assertContains(response, "Seña del turno: $2250")
        self.assertContains(response, "Saldo en el club: $5250")
        self.assertContains(response, "padel.principal")
        self.assertContains(response, "Pago pendiente")

    def test_price_shown_in_booking_form(self):
        self.complex.booking_price = Decimal("6000")
        self.complex.save(update_fields=["booking_price"])
        response = self.client.get(
            reverse("turnos"),
            {"complejo": self.complex.slug, "fecha": self.day.isoformat(), "cancha": self.court.pk, "hora": "09:00"},
        )
        self.assertContains(response, "Precio del turno: <strong>$6000")
        self.assertContains(response, "Seña: <strong>$1800")
        self.assertContains(response, "Saldo en el club: $4200")
        self.assertContains(response, "Reservar con seña pendiente")

    def test_confirmation_shows_whatsapp_button(self):
        self.complex.whatsapp_phone = "221 555 1234"
        self.complex.save(update_fields=["whatsapp_phone"])
        self.reserve()
        booking = CourtBooking.objects.get()
        response = self.client.get(reverse("turno_confirmado", args=[booking.code]))
        # Los números locales de 10 dígitos se normalizan con el 549.
        self.assertContains(response, "https://wa.me/5492215551234")
        self.assertContains(response, "Confirmar por WhatsApp")

    def test_no_whatsapp_button_without_complex_phone(self):
        self.reserve()
        booking = CourtBooking.objects.get()
        response = self.client.get(reverse("turno_confirmado", args=[booking.code]))
        self.assertNotContains(response, "wa.me")

    def test_booking_notifies_the_complex_whatsapp(self):
        self.complex.whatsapp_phone = "5492215551111"
        self.complex.whatsapp_apikey = "1234"
        self.complex.save(update_fields=["whatsapp_phone", "whatsapp_apikey"])
        with mock.patch("core.notifications._send_whatsapp") as sender:
            self.reserve()
        booking = CourtBooking.objects.get()
        sender.assert_called_once()
        args, kwargs = sender.call_args
        self.assertEqual(kwargs.get("phone"), "5492215551111")
        self.assertEqual(kwargs.get("apikey"), "1234")
        self.assertIn(booking.court.name, args[0])
        self.assertIn(booking.code, args[0])

    def test_panel_mark_paid_and_day_totals(self):
        self.complex.booking_price = Decimal("8000")
        self.complex.save(update_fields=["booking_price"])
        admin = User.objects.create_superuser("admin-cobros", password="clave-segura-123")
        self.client.force_login(admin)
        self.client.post(
            reverse("panel_turno_accion"),
            {
                "complejo": self.complex.slug,
                "fecha": self.day.isoformat(),
                "accion": "reservar",
                "cancha": self.court.pk,
                "hora": "09:00",
                "nombre": "Cliente Panel",
                "telefono": "2215550000",
                "nota": "",
            },
        )
        booking = CourtBooking.objects.get()
        self.assertEqual(booking.price, Decimal("8000.00"))

        # Sin cobrar: la agenda muestra el total reservado y el estado pendiente.
        response = self.client.get(
            reverse("panel_turnos"), {"complejo": self.complex.slug, "fecha": self.day.isoformat()}
        )
        self.assertContains(response, "Reservado del día")
        self.assertContains(response, "Pendiente")

        self.client.post(
            reverse("panel_turno_accion"),
            {
                "complejo": self.complex.slug,
                "fecha": self.day.isoformat(),
                "accion": "marcar_pagado",
                "turno_id": booking.pk,
            },
        )
        booking.refresh_from_db()
        self.assertTrue(booking.is_paid)
        response = self.client.get(
            reverse("panel_turnos"), {"complejo": self.complex.slug, "fecha": self.day.isoformat()}
        )
        self.assertContains(response, "Abonado")
        self.assertContains(response, "$8000")

    def test_panel_whatsapp_notify_link_for_customer(self):
        admin = User.objects.create_superuser("admin-avisos", password="clave-segura-123")
        self.client.force_login(admin)
        self.make_booking(
            self.complex, self.court, self.day, time(10, 30), customer_name="Ana", customer_phone="2215551234"
        )
        response = self.client.get(
            reverse("panel_turnos"), {"complejo": self.complex.slug, "fecha": self.day.isoformat()}
        )
        self.assertContains(response, "https://wa.me/5492215551234")
        self.assertContains(response, "Avisar")

    def test_ajustes_saved_from_panel(self):
        admin = User.objects.create_superuser("admin-ajustes", password="clave-segura-123")
        self.client.force_login(admin)
        response = self.client.post(
            reverse("panel_horarios_accion"),
            {
                "complejo": self.complex.slug,
                "accion": "guardar_ajustes",
                "precio": "8000,50",
                "mp_alias": "padel.principal",
                "mp_holder": "Juan Pérez",
                "mp_link": "mpago.la/abc",
                "whatsapp_phone": "5492215551234",
                "whatsapp_apikey": "1234",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.complex.refresh_from_db()
        self.assertEqual(self.complex.booking_price, Decimal("8000.50"))
        self.assertEqual(self.complex.mp_alias, "padel.principal")
        self.assertEqual(self.complex.mp_holder, "Juan Pérez")
        self.assertEqual(self.complex.mp_link, "https://mpago.la/abc")
        self.assertEqual(self.complex.whatsapp_phone, "5492215551234")
        self.assertEqual(self.complex.whatsapp_apikey, "1234")

    def test_ajustes_reject_invalid_price(self):
        admin = User.objects.create_superuser("admin-ajustes-2", password="clave-segura-123")
        self.client.force_login(admin)
        response = self.client.post(
            reverse("panel_horarios_accion"),
            {"complejo": self.complex.slug, "accion": "guardar_ajustes", "precio": "-3"},
        )
        self.assertEqual(response.status_code, 302)
        self.complex.refresh_from_db()
        self.assertEqual(self.complex.booking_price, Decimal("0.00"))

    def test_ajustes_page_requires_login(self):
        response = self.client.get(reverse("panel_turnos_ajustes"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response.url)

    def test_ajustes_page_renders(self):
        admin = User.objects.create_superuser("admin-ajustes-3", password="clave-segura-123")
        self.client.force_login(admin)
        response = self.client.get(reverse("panel_turnos_ajustes"), {"complejo": self.complex.slug})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Precio y avisos")
        self.assertContains(response, "Guardar ajustes")


@override_settings(
    MERCADO_PAGO_ONLINE_PAYMENTS_ENABLED=True,
    MERCADO_PAGO_REQUIRE_WEBHOOK_SECRET=False,
)
class MercadoPagoDepositTests(TurnosFixtureMixin, TestCase):
    """Seña obligatoria del 30% con Checkout Pro y confirmación automática.

    La mayoría de estas pruebas usa el modo histórico sin secreto para aislar
    el flujo de pagos. Las pruebas de ``fail-closed`` activan explícitamente la
    exigencia de firma, que es el valor por defecto y obligatorio en producción.
    """

    def setUp(self):
        self.complex = self.make_complex(booking_price=Decimal("8000"))
        self.complex.mp_alias = "padel.principal"
        self.complex.mp_access_token = "APP_USR-12345678901234567890"
        self.complex.save(
            update_fields=["booking_price", "mp_alias", "mp_access_token_encrypted"]
        )
        self.court = self.make_court(self.complex)
        self.day = timezone.localdate() + timedelta(days=5)
        self.make_schedule(self.complex, day_type=schedule_service.day_type_for(self.day))

    def reserve(self, **overrides):
        data = {
            "complejo": self.complex.slug,
            "fecha": self.day.isoformat(),
            "cancha": self.court.pk,
            "hora": "09:00",
            "nombre": "Ana Pérez",
            "telefono": "2215551234",
            "nota": "",
        }
        data.update(overrides)
        return self.client.post(reverse("turno_reservar"), data)

    def make_pending(self):
        with mock.patch("core.views.notify_court_booking"):
            self.reserve()
        return CourtBooking.objects.get()

    def payment_payload(self, booking, status="approved", amount="2400.00", reference=None):
        return {
            "id": "payment-123",
            "status": status,
            "external_reference": reference if reference is not None else booking.code,
            "transaction_amount": amount,
            "currency_id": "ARS",
        }

    def test_public_booking_holds_slot_and_requires_deposit(self):
        with mock.patch("core.views.notify_court_booking") as notify:
            response = self.reserve()
        booking = CourtBooking.objects.get()
        self.assertEqual(response.url, reverse("turno_confirmado", args=[booking.code]))
        self.assertEqual(booking.status, "pending_payment")
        self.assertEqual(booking.deposit_amount, Decimal("2400.00"))
        self.assertFalse(booking.is_paid)
        self.assertIsNotNone(booking.payment_expires_at)
        notify.assert_not_called()

    def test_confirmation_shows_deposit_balance_and_pay_button(self):
        booking = self.make_pending()
        response = self.client.get(reverse("turno_confirmado", args=[booking.code]))
        self.assertContains(response, "Seña del turno: $2400")
        self.assertContains(response, "Saldo en el club: $5600")
        self.assertContains(response, "Pagar seña con Mercado Pago")
        self.assertContains(response, "Seña pendiente")

    def test_pay_view_creates_preference_and_redirects(self):
        booking = self.make_pending()
        with mock.patch("core.views.payments_service.create_checkout_preference") as create:
            create.return_value = {"id": "pref-1", "init_point": "https://mp.test/checkout"}
            response = self.client.get(reverse("turno_pagar", args=[booking.code]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://mp.test/checkout")
        booking.refresh_from_db()
        self.assertEqual(booking.payment_preference_id, "pref-1")
        self.assertEqual(booking.payment_init_point, "https://mp.test/checkout")
        args, _ = create.call_args
        self.assertEqual(args[0], "APP_USR-12345678901234567890")
        payload = args[1]
        self.assertEqual(payload["external_reference"], booking.code)
        self.assertEqual(payload["items"][0]["unit_price"], 2400.0)
        self.assertEqual(payload["items"][0]["currency_id"], "ARS")
        self.assertTrue(payload["expires"])
        self.assertLess(payload["expiration_date_from"], payload["expiration_date_to"])
        self.assertTrue(payload["notification_url"].endswith(f"/turnos/pago/mercadopago/{self.complex.slug}/"))
        self.assertTrue(payload["back_urls"]["success"].endswith(f"/turnos/{booking.code}/pago/retorno/"))
        self.assertEqual(payload["payment_methods"]["excluded_payment_types"], [{"id": "ticket"}, {"id": "atm"}])

    def test_pay_view_reuses_the_existing_preference(self):
        booking = self.make_pending()
        with mock.patch("core.views.payments_service.create_checkout_preference") as create:
            create.return_value = {"id": "pref-1", "init_point": "https://mp.test/checkout"}
            self.client.get(reverse("turno_pagar", args=[booking.code]))
            response = self.client.get(reverse("turno_pagar", args=[booking.code]))
        self.assertEqual(response.url, "https://mp.test/checkout")
        self.assertEqual(create.call_count, 1)

    @override_settings(MERCADO_PAGO_CURRENCY="UYU")
    def test_checkout_uses_the_configured_currency(self):
        booking = self.make_pending()
        with mock.patch("core.views.payments_service.create_checkout_preference") as create:
            create.return_value = {"id": "pref-1", "init_point": "https://mp.test/checkout"}
            self.client.get(reverse("turno_pagar", args=[booking.code]))
        payload = create.call_args.args[1]
        self.assertEqual(payload["items"][0]["currency_id"], "UYU")

    def test_checkout_and_pay_button_are_blocked_without_webhook_secret(self):
        booking = self.make_pending()
        with override_settings(MERCADO_PAGO_REQUIRE_WEBHOOK_SECRET=True):
            self.assertFalse(self.complex.online_payments_enabled)
            response = self.client.get(
                reverse("turno_confirmado", args=[booking.code])
            )
            self.assertNotContains(response, "Pagar seña con Mercado Pago")
            with mock.patch(
                "core.views.payments_service.create_checkout_preference"
            ) as create:
                response = self.client.get(reverse("turno_pagar", args=[booking.code]))
            self.assertEqual(response.status_code, 302)
            create.assert_not_called()
        booking.refresh_from_db()
        self.assertEqual(booking.payment_preference_id, "")

    def test_public_reservation_falls_back_to_manual_without_webhook_secret(self):
        with override_settings(MERCADO_PAGO_REQUIRE_WEBHOOK_SECRET=True):
            with mock.patch("core.views.notify_court_booking"):
                self.reserve()
        booking = CourtBooking.objects.get()
        self.assertEqual(booking.status, "booked")
        self.assertIsNone(booking.payment_expires_at)
        self.assertEqual(booking.payment_status, "")

    def test_webhook_fails_closed_when_the_secret_is_required_but_missing(self):
        booking = self.make_pending()
        with override_settings(MERCADO_PAGO_REQUIRE_WEBHOOK_SECRET=True), mock.patch(
            "core.views.payments_service.fetch_payment"
        ) as fetch:
            response = self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 503)
        fetch.assert_not_called()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending_payment")
        self.assertFalse(booking.is_paid)

    def test_webhook_accepts_a_valid_signature_when_the_secret_is_required(self):
        secret = "secreto-webhook-1234567890"
        request_id = "request-abc"
        self.complex.mp_webhook_secret = secret
        self.complex.save(update_fields=["mp_webhook_secret_encrypted"])
        booking = self.make_pending()
        with override_settings(MERCADO_PAGO_REQUIRE_WEBHOOK_SECRET=True), mock.patch(
            "core.views.payments_service.fetch_payment"
        ) as fetch, mock.patch("core.views.notify_court_booking"):
            fetch.return_value = self.payment_payload(booking)
            response = self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
                HTTP_X_REQUEST_ID=request_id,
                HTTP_X_SIGNATURE=self.signature(secret, "payment-123", request_id),
            )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertTrue(booking.is_paid)
        self.assertEqual(booking.status, "booked")

    def test_webhook_approves_and_marks_paid_once(self):
        booking = self.make_pending()
        with mock.patch("core.views.payments_service.fetch_payment") as fetch, mock.patch(
            "core.views.notify_court_booking"
        ) as notify:
            fetch.return_value = self.payment_payload(booking)
            response = self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)
            booking.refresh_from_db()
            self.assertEqual(booking.status, "booked")
            self.assertTrue(booking.is_paid)
            self.assertEqual(booking.payment_id, "payment-123")
            self.assertEqual(booking.payment_status, "approved")
            self.assertIsNotNone(booking.paid_at)
            self.assertIsNone(booking.payment_expires_at)
            notify.assert_called_once()
            self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
            )
            notify.assert_called_once()
        deposit = CourtPayment.objects.get(booking=booking, kind="deposit")
        self.assertEqual(deposit.amount, Decimal("2400.00"))
        self.assertEqual(deposit.method, "mercadopago")
        self.assertEqual(deposit.external_id, "payment-123")
        self.assertEqual(CourtPayment.objects.filter(booking=booking).count(), 1)

    def test_webhook_refund_offsets_the_deposit_in_cash(self):
        booking = self.make_pending()
        with mock.patch("core.views.payments_service.fetch_payment") as fetch, mock.patch(
            "core.views.notify_court_booking"
        ):
            fetch.return_value = self.payment_payload(booking)
            self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
            )
            fetch.return_value = self.payment_payload(booking, status="refunded")
            for _ in range(2):
                self.client.post(
                    reverse("turno_pago_webhook", args=[self.complex.slug]),
                    data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                    content_type="application/json",
                )
        booking.refresh_from_db()
        self.assertFalse(booking.is_paid)
        self.assertEqual(booking.payment_status, "refunded")
        self.assertEqual(CourtPayment.objects.filter(booking=booking, kind="refund").count(), 1)
        refund = CourtPayment.objects.get(booking=booking, kind="refund")
        self.assertEqual(refund.amount, Decimal("2400.00"))
        self.assertEqual(refund.method, "mercadopago")
        self.assertEqual(refund.external_id, "refund:payment-123")
        summary = cash_service.summary_for_day(self.complex, timezone.localdate())
        self.assertEqual(summary["total_income"], Decimal("2400.00"))
        self.assertEqual(summary["total_outgoing"], Decimal("2400.00"))
        self.assertEqual(summary["net_total"], Decimal("0.00"))

    def test_webhook_in_process_then_approved(self):
        booking = self.make_pending()
        with mock.patch("core.views.payments_service.fetch_payment") as fetch:
            fetch.return_value = self.payment_payload(booking, status="in_process")
            self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
            )
            booking.refresh_from_db()
            self.assertEqual(booking.status, "pending_payment")
            self.assertFalse(booking.is_paid)
            self.assertEqual(booking.payment_status, "in_process")

            fetch.return_value = self.payment_payload(booking)
            self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
            )
        booking.refresh_from_db()
        self.assertEqual(booking.status, "booked")
        self.assertTrue(booking.is_paid)

    def test_webhook_rejects_a_different_amount(self):
        booking = self.make_pending()
        with mock.patch("core.views.payments_service.fetch_payment") as fetch:
            fetch.return_value = self.payment_payload(booking, amount="100.00")
            self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
            )
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending_payment")
        self.assertFalse(booking.is_paid)
        self.assertEqual(booking.payment_status, "amount_mismatch")
        # El dinero acreditado no se oculta: queda en caja para revisión,
        # aunque no alcance para confirmar la seña.
        self.assertEqual(booking.paid_amount, Decimal("100.00"))
        self.assertEqual(
            CourtPayment.objects.filter(booking=booking, kind="deposit").count(), 1
        )

    def test_webhook_ignores_another_reference(self):
        booking = self.make_pending()
        with mock.patch("core.views.payments_service.fetch_payment") as fetch:
            fetch.return_value = self.payment_payload(booking, reference="OTRO-TURNO")
            self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
            )
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending_payment")
        self.assertFalse(booking.is_paid)

    def signature(self, secret, payment_id, request_id, timestamp="1704908010"):
        manifest = f"id:{payment_id};request-id:{request_id};ts:{timestamp};"
        digest = hmac.new(
            secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"ts={timestamp},v1={digest}"

    def test_webhook_rejects_missing_signature_when_the_secret_is_configured(self):
        self.complex.mp_webhook_secret = "secreto-webhook-1234567890"
        self.complex.save(update_fields=["mp_webhook_secret_encrypted"])
        booking = self.make_pending()
        with mock.patch("core.views.payments_service.fetch_payment") as fetch:
            response = self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 401)
        fetch.assert_not_called()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending_payment")
        self.assertFalse(booking.is_paid)

    def test_webhook_accepts_a_valid_signature(self):
        secret = "secreto-webhook-1234567890"
        request_id = "request-abc"
        self.complex.mp_webhook_secret = secret
        self.complex.save(update_fields=["mp_webhook_secret_encrypted"])
        booking = self.make_pending()
        with mock.patch("core.views.payments_service.fetch_payment") as fetch, mock.patch(
            "core.views.notify_court_booking"
        ):
            fetch.return_value = self.payment_payload(booking)
            response = self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
                HTTP_X_REQUEST_ID=request_id,
                HTTP_X_SIGNATURE=self.signature(secret, "payment-123", request_id),
            )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "booked")
        self.assertTrue(booking.is_paid)

    def test_webhook_rejects_an_invalid_signature(self):
        self.complex.mp_webhook_secret = "secreto-webhook-1234567890"
        self.complex.save(update_fields=["mp_webhook_secret_encrypted"])
        booking = self.make_pending()
        with mock.patch("core.views.payments_service.fetch_payment") as fetch:
            response = self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
                HTTP_X_REQUEST_ID="request-abc",
                HTTP_X_SIGNATURE="ts=1704908010,v1=00",
            )
        self.assertEqual(response.status_code, 401)
        fetch.assert_not_called()
        booking.refresh_from_db()
        self.assertFalse(booking.is_paid)

    def test_webhook_fails_closed_if_the_saved_secret_cannot_be_decrypted(self):
        self.complex.mp_webhook_secret = "secreto-webhook-1234567890"
        self.complex.save(update_fields=["mp_webhook_secret_encrypted"])
        Complex.objects.filter(pk=self.complex.pk).update(
            mp_webhook_secret_encrypted="ciphertext-invalido"
        )
        booking = self.make_pending()
        with mock.patch("core.views.payments_service.fetch_payment") as fetch:
            response = self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
                HTTP_X_REQUEST_ID="request-abc",
                HTTP_X_SIGNATURE="ts=1704908010,v1=00",
            )
        self.assertEqual(response.status_code, 401)
        fetch.assert_not_called()
        booking.refresh_from_db()
        self.assertFalse(booking.is_paid)

    def test_merchant_order_notification_processes_its_payments(self):
        booking = self.make_pending()
        with mock.patch(
            "core.views.payments_service.fetch_merchant_order"
        ) as fetch_order, mock.patch(
            "core.views.payments_service.fetch_payment"
        ) as fetch_payment, mock.patch("core.views.notify_court_booking"):
            fetch_order.return_value = {"payments": [{"id": "payment-123"}]}
            fetch_payment.return_value = self.payment_payload(booking)
            response = self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps(
                    {"type": "topic_merchant_order_wh", "data": {"id": "order-123"}}
                ),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "booked")
        self.assertTrue(booking.is_paid)
        fetch_order.assert_called_once()
        fetch_payment.assert_called_once_with(
            self.complex.mp_access_token, "payment-123"
        )

    def test_chargeback_notification_marks_the_booking_for_manual_review(self):
        booking = self.make_pending()
        with mock.patch(
            "core.views.payments_service.fetch_chargeback"
        ) as fetch_chargeback, mock.patch(
            "core.views.payments_service.fetch_payment"
        ) as fetch_payment:
            fetch_chargeback.return_value = {
                "id": "chargeback-1",
                "payment_id": "payment-123",
                "status": "in_process",
            }
            fetch_payment.return_value = self.payment_payload(booking)
            response = self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps(
                    {
                        "type": "topic_chargebacks_wh",
                        "data": {"id": "chargeback-1", "payment_id": "payment-123"},
                    }
                ),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.payment_status, "charged_back")
        self.assertFalse(booking.is_paid)
        self.assertFalse(CourtPayment.objects.filter(booking=booking).exists())
        self.assertTrue(
            any(
                "sin movimiento de contracargo" in issue
                for issue in cash_service.audit_booking(booking)
            )
        )

    def test_chargeback_transient_failure_returns_retry_status(self):
        booking = self.make_pending()
        with mock.patch(
            "core.views.payments_service.fetch_chargeback"
        ) as fetch_chargeback, mock.patch(
            "core.views.payments_service.fetch_payment"
        ) as fetch_payment:
            fetch_chargeback.return_value = {
                "id": "chargeback-1",
                "payment_id": "payment-123",
            }
            fetch_payment.side_effect = payments_service.MercadoPagoError(
                "temporal", status_code=500
            )
            response = self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps(
                    {"type": "topic_chargebacks_wh", "data": {"id": "chargeback-1"}}
                ),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 502)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending_payment")

    def test_return_from_checkout_rejects_a_payment_without_reference(self):
        booking = self.make_pending()
        payload = self.payment_payload(booking, reference="")
        with mock.patch("core.views.payments_service.fetch_payment") as fetch:
            fetch.return_value = payload
            response = self.client.get(
                reverse("turno_pago_retorno", args=[booking.code]) + "?payment_id=payment-123"
            )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertFalse(booking.is_paid)
        self.assertEqual(booking.status, "pending_payment")

    def test_expired_hold_is_released_and_slot_can_be_reserved_again(self):
        booking = self.make_pending()
        booking.payment_expires_at = timezone.now() - timedelta(minutes=1)
        booking.save(update_fields=["payment_expires_at"])
        self.client.get(
            reverse("turnos"), {"complejo": self.complex.slug, "fecha": self.day.isoformat()}
        )
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        self.assertEqual(booking.payment_status, "expired")
        self.reserve(nombre="Otra Persona")
        self.assertEqual(CourtBooking.objects.filter(status="pending_payment").count(), 1)
        self.assertEqual(CourtBooking.objects.count(), 2)

    def test_expired_hold_cannot_start_a_payment(self):
        booking = self.make_pending()
        booking.payment_expires_at = timezone.now() - timedelta(minutes=1)
        booking.save(update_fields=["payment_expires_at"])
        with mock.patch("core.views.payments_service.create_checkout_preference") as create:
            response = self.client.get(reverse("turno_pagar", args=[booking.code]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("turno_confirmado", args=[booking.code]))
        create.assert_not_called()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")

    def test_approved_payment_after_expiry_restores_a_free_slot(self):
        booking = self.make_pending()
        booking.payment_expires_at = timezone.now() - timedelta(minutes=1)
        booking.save(update_fields=["payment_expires_at"])
        CourtBooking.expire_pending()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        with mock.patch("core.views.payments_service.fetch_payment") as fetch, mock.patch(
            "core.views.notify_court_booking"
        ) as notify:
            fetch.return_value = self.payment_payload(booking)
            self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
            )
            notify.assert_called_once()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "booked")
        self.assertTrue(booking.is_paid)

    def test_payment_after_client_cancellation_is_flagged_without_reopening(self):
        booking = self.make_pending()
        self.client.post(reverse("turno_cancelar", args=[booking.code]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        self.assertEqual(booking.payment_status, "cancelled")
        with mock.patch("core.views.payments_service.fetch_payment") as fetch:
            fetch.return_value = self.payment_payload(booking)
            self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps({"type": "payment", "data": {"id": "payment-123"}}),
                content_type="application/json",
            )
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        self.assertTrue(booking.is_paid)
        self.assertEqual(booking.payment_status, "approved_conflict")

    def test_return_from_checkout_verifies_the_payment(self):
        booking = self.make_pending()
        with mock.patch("core.views.payments_service.fetch_payment") as fetch:
            fetch.return_value = self.payment_payload(booking)
            response = self.client.get(
                reverse("turno_pago_retorno", args=[booking.code]) + "?payment_id=payment-123"
            )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertTrue(booking.is_paid)
        self.assertEqual(booking.status, "booked")

    def test_manual_payment_confirms_a_pending_hold(self):
        booking = self.make_pending()
        admin = User.objects.create_superuser("admin-seña", password="clave-segura-123")
        self.client.force_login(admin)
        self.client.post(
            reverse("panel_turno_accion"),
            {
                "complejo": self.complex.slug,
                "fecha": self.day.isoformat(),
                "accion": "marcar_pagado",
                "turno_id": booking.pk,
            },
        )
        booking.refresh_from_db()
        self.assertEqual(booking.status, "booked")
        self.assertTrue(booking.is_paid)
        self.assertEqual(booking.payment_status, "manual")
        self.assertIsNone(booking.payment_expires_at)

    def test_panel_charges_the_club_balance_after_the_deposit(self):
        booking = self.make_pending()
        admin = User.objects.create_superuser("admin-saldo", password="clave-segura-123")
        self.client.force_login(admin)
        base = {
            "complejo": self.complex.slug,
            "fecha": self.day.isoformat(),
            "accion": "marcar_pagado",
            "turno_id": booking.pk,
        }
        response = self.client.get(
            reverse("panel_turnos"),
            {"complejo": self.complex.slug, "fecha": self.day.isoformat()},
        )
        self.assertContains(response, "Seña $2400")
        self.assertContains(response, "Saldo $5600 en club")

        self.client.post(reverse("panel_turno_accion"), base)
        booking.refresh_from_db()
        self.assertTrue(booking.is_paid)
        self.assertFalse(booking.balance_paid)
        self.assertEqual(booking.paid_amount, Decimal("2400.00"))
        self.assertEqual(booking.balance_amount, Decimal("5600.00"))

        response = self.client.get(
            reverse("panel_turnos"),
            {"complejo": self.complex.slug, "fecha": self.day.isoformat()},
        )
        self.assertContains(response, "Seña abonada")
        self.assertContains(response, "Saldo $5600 en club")

        self.client.post(reverse("panel_turno_accion"), base)
        booking.refresh_from_db()
        self.assertTrue(booking.balance_paid)
        self.assertIsNotNone(booking.balance_paid_at)
        self.assertEqual(booking.paid_amount, Decimal("8000.00"))
        self.assertEqual(booking.balance_amount, Decimal("0.00"))

        response = self.client.get(
            reverse("panel_turnos"),
            {"complejo": self.complex.slug, "fecha": self.day.isoformat()},
        )
        self.assertContains(response, "Cobrado del día")
        self.assertContains(response, "$8000")

    def test_free_booking_does_not_require_a_deposit(self):
        self.complex.booking_price = Decimal("0")
        self.complex.save(update_fields=["booking_price"])
        self.reserve()
        booking = CourtBooking.objects.get()
        self.assertEqual(booking.status, "booked")
        self.assertEqual(booking.deposit_amount, Decimal("0"))
        self.assertTrue(booking.is_paid)
        response = self.client.get(reverse("turno_confirmado", args=[booking.code]))
        self.assertNotContains(response, "Seña del turno")

    def test_without_token_the_manual_flow_still_works(self):
        self.complex.mp_access_token = ""
        self.complex.save(update_fields=["mp_access_token_encrypted"])
        self.reserve()
        booking = CourtBooking.objects.get()
        self.assertEqual(booking.status, "booked")
        self.assertFalse(booking.is_paid)
        response = self.client.get(reverse("turno_confirmado", args=[booking.code]))
        self.assertContains(response, "Seña del turno: $2400")
        self.assertContains(response, "padel.principal")

    def test_deposit_rounding(self):
        self.assertEqual(
            payments_service.deposit_amount_for(Decimal("7500.00")), Decimal("2250.00")
        )
        self.assertEqual(
            payments_service.deposit_amount_for(Decimal("99.99")), Decimal("30.00")
        )

    def test_settings_save_and_clear_the_token(self):
        admin = User.objects.create_superuser("admin-token", password="clave-segura-123")
        self.client.force_login(admin)
        token = "APP_USR-99999999999999999999"
        response = self.client.post(
            reverse("panel_horarios_accion"),
            {
                "complejo": self.complex.slug,
                "accion": "guardar_ajustes",
                "precio": "8000",
                "mp_access_token": token,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.complex.refresh_from_db()
        self.assertTrue(self.complex.has_mp_token)
        self.assertEqual(self.complex.mp_access_token, token)
        self.assertNotIn(token, self.complex.mp_access_token_encrypted)

        self.client.post(
            reverse("panel_horarios_accion"),
            {
                "complejo": self.complex.slug,
                "accion": "guardar_ajustes",
                "precio": "8000",
                "mp_access_token": "",
            },
        )
        self.complex.refresh_from_db()
        self.assertTrue(self.complex.has_mp_token)

        self.client.post(
            reverse("panel_horarios_accion"),
            {
                "complejo": self.complex.slug,
                "accion": "guardar_ajustes",
                "precio": "8000",
                "mp_clear_token": "on",
            },
        )
        self.complex.refresh_from_db()
        self.assertFalse(self.complex.has_mp_token)

    def test_settings_warn_when_the_token_has_no_webhook_secret(self):
        admin = User.objects.create_superuser("admin-secreto", password="clave-segura-123")
        self.client.force_login(admin)
        with override_settings(MERCADO_PAGO_REQUIRE_WEBHOOK_SECRET=True):
            response = self.client.post(
                reverse("panel_horarios_accion"),
                {
                    "complejo": self.complex.slug,
                    "accion": "guardar_ajustes",
                    "precio": "8000",
                },
                follow=True,
            )
        self.assertContains(response, "falta el secreto")
        self.complex.refresh_from_db()
        with override_settings(MERCADO_PAGO_REQUIRE_WEBHOOK_SECRET=True):
            self.assertFalse(self.complex.online_payments_enabled)

    def test_settings_save_and_clear_the_webhook_secret(self):
        admin = User.objects.create_superuser("admin-webhook", password="clave-segura-123")
        self.client.force_login(admin)
        secret = "secreto-webhook-1234567890"
        response = self.client.post(
            reverse("panel_horarios_accion"),
            {
                "complejo": self.complex.slug,
                "accion": "guardar_ajustes",
                "precio": "8000",
                "mp_webhook_secret": secret,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.complex.refresh_from_db()
        self.assertTrue(self.complex.has_mp_webhook_secret)
        self.assertEqual(self.complex.mp_webhook_secret, secret)
        self.assertNotIn(secret, self.complex.mp_webhook_secret_encrypted)

        self.client.post(
            reverse("panel_horarios_accion"),
            {
                "complejo": self.complex.slug,
                "accion": "guardar_ajustes",
                "precio": "8000",
                "mp_clear_webhook_secret": "on",
            },
        )
        self.complex.refresh_from_db()
        self.assertFalse(self.complex.has_mp_webhook_secret)

    def test_settings_show_webhook_secret_status_without_the_secret(self):
        self.complex.mp_webhook_secret = "secreto-webhook-1234567890"
        self.complex.save(update_fields=["mp_webhook_secret_encrypted"])
        admin = User.objects.create_superuser("admin-webhook-ui", password="clave-segura-123")
        self.client.force_login(admin)
        response = self.client.get(
            reverse("panel_turnos_ajustes"), {"complejo": self.complex.slug}
        )
        self.assertContains(response, "Secreto de firma del webhook")
        self.assertContains(response, "Dejalo vacío para conservar el secreto")
        self.assertNotContains(response, "secreto-webhook-1234567890")

    def test_settings_show_token_status_without_the_secret(self):
        admin = User.objects.create_superuser("admin-token-ui", password="clave-segura-123")
        self.client.force_login(admin)
        response = self.client.get(
            reverse("panel_turnos_ajustes"), {"complejo": self.complex.slug}
        )
        self.assertContains(response, "Token cargado")
        self.assertContains(response, "El campo de abajo siempre aparece vacío")
        self.assertContains(response, "Dejalo vacío para conservar el token")
        self.assertNotContains(response, "APP_USR-12345678901234567890")

    def test_settings_reject_a_payment_link_as_token(self):
        self.complex.mp_access_token = ""
        self.complex.save(update_fields=["mp_access_token_encrypted"])
        admin = User.objects.create_superuser("admin-link", password="clave-segura-123")
        self.client.force_login(admin)
        response = self.client.post(
            reverse("panel_horarios_accion"),
            {
                "complejo": self.complex.slug,
                "accion": "guardar_ajustes",
                "precio": "8000",
                "mp_access_token": "https://www.mercadopago.com.ar/checkout/v1/redirect?pref_id=123",
            },
            follow=True,
        )
        self.assertContains(response, "no es un access token")
        self.complex.refresh_from_db()
        self.assertFalse(self.complex.has_mp_token)

    def test_invalid_stored_token_is_ignored(self):
        self.complex.mp_access_token = "https://www.mercadopago.com.ar/link-de-pago"
        self.complex.save(update_fields=["mp_access_token_encrypted"])
        self.complex.refresh_from_db()
        self.assertFalse(self.complex.has_mp_token)


class CajaTurnosTests(TurnosFixtureMixin, TestCase):
    """Libro de caja, medios de pago, devoluciones y cierre diario."""

    def setUp(self):
        self.complex = self.make_complex(booking_price=Decimal("8000"))
        self.court = self.make_court(self.complex)
        self.day = timezone.localdate() + timedelta(days=5)
        self.make_schedule(self.complex, day_type=schedule_service.day_type_for(self.day))
        self.admin = User.objects.create_superuser("admin-caja", password="clave-segura-123")
        self.booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(9, 0),
            customer_name="Ana Caja",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
        )

    def record(self, kind="deposit", method="cash", amount="2400.00", booking=None):
        return cash_service.record_payment(
            booking or self.booking,
            kind=kind,
            method=method,
            amount=Decimal(amount),
            user=self.admin,
            paid_at=timezone.now(),
            note="Movimiento de prueba",
        )

    def test_caja_requires_login(self):
        response = self.client.get(reverse("panel_turnos_caja"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response.url)

    def test_caja_shows_totals_and_does_not_mix_complexes(self):
        self.record(kind="deposit", method="cash", amount="2400.00")
        self.record(kind="balance", method="transfer", amount="5600.00")
        cash_service.record_manual_refund(
            self.booking, Decimal("1000.00"), "cash", self.admin, note="Lluvia"
        )
        other_complex = self.make_complex(slug="otro-caja", name="Otro Complejo")
        other_court = self.make_court(other_complex, name="Cancha Otra")
        other_booking = self.make_booking(
            other_complex,
            other_court,
            self.day,
            time(10, 30),
            customer_name="No Debe Aparecer",
            price=Decimal("9999"),
            deposit_amount=Decimal("3000"),
        )
        cash_service.record_payment(
            other_booking,
            kind="deposit",
            method="cash",
            amount=Decimal("9999"),
            user=self.admin,
        )

        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("panel_turnos_caja"),
            {
                "complejo": self.complex.slug,
                "desde": timezone.localdate().isoformat(),
                "hasta": timezone.localdate().isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Caja y contabilidad")
        self.assertContains(response, "$8000")
        self.assertContains(response, "$1000")
        self.assertContains(response, "$7000")
        self.assertContains(response, "Efectivo")
        self.assertContains(response, "Transferencia")
        self.assertContains(response, "Ana Caja")
        self.assertNotContains(response, "No Debe Aparecer")
        self.assertNotContains(response, "$9999")

    def test_export_csv_contains_movements_and_totals(self):
        self.record(kind="deposit", method="cash", amount="2400.00")
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("panel_turnos_caja_export"),
            {
                "complejo": self.complex.slug,
                "desde": timezone.localdate().isoformat(),
                "hasta": timezone.localdate().isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        content = response.content.decode("utf-8-sig")
        self.assertIn("Fecha de caja;Tipo;Medio", content)
        self.assertIn(self.booking.code, content)
        self.assertIn("Seña", content)
        self.assertIn("2400.00", content)

    def test_manual_payment_records_method_and_reversal_keeps_history(self):
        self.client.force_login(self.admin)
        base = {
            "complejo": self.complex.slug,
            "fecha": self.day.isoformat(),
            "accion": "marcar_pagado",
            "turno_id": self.booking.pk,
        }
        self.client.post(reverse("panel_turno_accion"), {**base, "metodo": "transfer"})
        self.booking.refresh_from_db()
        self.assertTrue(self.booking.is_paid)
        deposit = CourtPayment.objects.get(booking=self.booking, kind="deposit")
        self.assertEqual(deposit.amount, Decimal("2400.00"))
        self.assertEqual(deposit.method, "transfer")
        self.assertEqual(deposit.collected_by, self.admin)

        self.client.post(reverse("panel_turno_accion"), {**base, "metodo": "card"})
        self.booking.refresh_from_db()
        self.assertTrue(self.booking.balance_paid)
        balance = CourtPayment.objects.get(booking=self.booking, kind="balance")
        self.assertEqual(balance.amount, Decimal("5600.00"))
        self.assertEqual(balance.method, "card")

        self.client.post(
            reverse("panel_turno_accion"),
            {**base, "accion": "marcar_pendiente"},
        )
        self.booking.refresh_from_db()
        self.assertFalse(self.booking.balance_paid)
        reversal = CourtPayment.objects.get(booking=self.booking, kind="reversal")
        self.assertEqual(reversal.amount, Decimal("5600.00"))
        self.assertEqual(reversal.reverses, balance)
        self.assertEqual(CourtPayment.objects.filter(booking=self.booking).count(), 3)

    def test_unmarking_a_deposit_adds_a_reversal_without_deleting_the_income(self):
        deposit = self.record(kind="deposit", method="transfer", amount="2400.00")
        self.booking.is_paid = True
        self.booking.paid_at = timezone.now()
        self.booking.payment_status = "manual"
        self.booking.save(update_fields=["is_paid", "paid_at", "payment_status"])
        self.client.force_login(self.admin)
        self.client.post(
            reverse("panel_turno_accion"),
            {
                "complejo": self.complex.slug,
                "fecha": self.day.isoformat(),
                "accion": "marcar_pendiente",
                "turno_id": self.booking.pk,
            },
        )
        self.booking.refresh_from_db()
        self.assertFalse(self.booking.is_paid)
        reversal = CourtPayment.objects.get(booking=self.booking, kind="reversal")
        self.assertEqual(reversal.amount, Decimal("2400.00"))
        self.assertEqual(reversal.method, "transfer")
        self.assertEqual(reversal.reverses, deposit)
        self.assertEqual(cash_service.refundable_amount(self.booking), Decimal("0.00"))

    def test_full_manual_booking_records_a_full_payment(self):
        full_booking = self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(10, 30),
            customer_name="Turno manual",
            price=Decimal("8000"),
            deposit_amount=Decimal("0"),
        )
        self.client.force_login(self.admin)
        self.client.post(
            reverse("panel_turno_accion"),
            {
                "complejo": self.complex.slug,
                "fecha": self.day.isoformat(),
                "accion": "marcar_pagado",
                "turno_id": full_booking.pk,
                "metodo": "cash",
            },
        )
        payment = CourtPayment.objects.get(booking=full_booking)
        self.assertEqual(payment.kind, "full")
        self.assertEqual(payment.amount, Decimal("8000.00"))
        self.assertEqual(payment.method, "cash")

    def test_manual_refund_validates_and_records_the_movement(self):
        self.record(kind="deposit", method="cash", amount="2400.00")
        self.client.force_login(self.admin)
        url = reverse("panel_turnos_caja_devolucion")
        response = self.client.post(
            url,
            {
                "complejo": self.complex.slug,
                "codigo": self.booking.code,
                "monto": "1000,50",
                "metodo": "cash",
                "nota": "Acuerdo con el cliente",
                "refund_token": "refund-token-0000000000000001",
            },
        )
        self.assertEqual(response.status_code, 302)
        refund = CourtPayment.objects.get(booking=self.booking, kind="refund")
        self.assertEqual(refund.amount, Decimal("1000.50"))
        self.assertEqual(refund.method, "cash")
        self.assertEqual(refund.collected_by, self.admin)
        self.assertEqual(cash_service.refundable_amount(self.booking), Decimal("1399.50"))

        response = self.client.post(
            url,
            {
                "complejo": self.complex.slug,
                "codigo": self.booking.code,
                "monto": "2000",
                "metodo": "cash",
                "refund_token": "refund-token-0000000000000002",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CourtPayment.objects.filter(booking=self.booking, kind="refund").count(), 1)

    def test_manual_refund_without_form_token_is_rejected(self):
        self.record(kind="deposit", method="cash", amount="2400.00")
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("panel_turnos_caja_devolucion"),
            {
                "complejo": self.complex.slug,
                "codigo": self.booking.code,
                "monto": "1000",
                "metodo": "cash",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            CourtPayment.objects.filter(booking=self.booking, kind="refund").exists()
        )

    def test_staff_of_another_complex_cannot_close_or_refund(self):
        other_complex = self.make_complex(slug="ajeno-caja", name="Complejo Ajeno")
        staff = User.objects.create_user("staff-caja", password="clave-segura-123")
        staff.complexes.add(other_complex)
        self.record(kind="deposit", method="cash", amount="2400.00")
        self.client.force_login(staff)
        response = self.client.post(
            reverse("panel_turnos_caja_cerrar"),
            {"complejo": self.complex.slug, "fecha": timezone.localdate().isoformat()},
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            reverse("panel_turnos_caja_devolucion"),
            {
                "complejo": self.complex.slug,
                "codigo": self.booking.code,
                "monto": "100",
                "metodo": "cash",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(CashClose.objects.count(), 0)

    def test_cash_close_snapshots_totals_and_cannot_repeat(self):
        self.record(kind="deposit", method="cash", amount="2400.00")
        self.client.force_login(self.admin)
        url = reverse("panel_turnos_caja_cerrar")
        today = timezone.localdate().isoformat()
        self.client.post(
            url,
            {"complejo": self.complex.slug, "fecha": today, "nota": "Cierre de prueba"},
        )
        close = CashClose.objects.get(complex=self.complex, date=timezone.localdate())
        self.assertEqual(close.total_income, Decimal("2400.00"))
        self.assertEqual(close.total_outgoing, Decimal("0.00"))
        self.assertEqual(close.net_total, Decimal("2400.00"))
        self.assertEqual(close.closed_by, self.admin)
        self.assertIn("cash", close.payment_breakdown)

        self.client.post(
            url,
            {"complejo": self.complex.slug, "fecha": today, "nota": "Repetido"},
        )
        self.assertEqual(CashClose.objects.filter(complex=self.complex).count(), 1)


class PublicCalendarViewTests(TurnosFixtureMixin, TestCase):
    def setUp(self):
        self.complex = self.make_complex()
        self.court = self.make_court(self.complex)
        self.today = timezone.localdate()
        self.make_schedule(self.complex, day_type=schedule_service.day_type_for(self.today))

    def test_calendar_renders_and_links_days(self):
        response = self.client.get(reverse("turnos"), {"complejo": self.complex.slug})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "calendar__grid")
        self.assertContains(response, f"fecha={self.today.isoformat()}")

    def test_calendar_marks_closed_days(self):
        CourtSchedule.objects.all().delete()
        for day_type, _ in CourtSchedule.DAY_TYPE:
            self.make_schedule(self.complex, day_type=day_type, is_open=False)
        response = self.client.get(reverse("turnos"), {"complejo": self.complex.slug})
        self.assertContains(response, "is-closed")

    def test_calendar_navigates_between_months(self):
        response = self.client.get(reverse("turnos"), {"complejo": self.complex.slug})
        next_month = (self.today.replace(day=1) + timedelta(days=32)).replace(day=1)
        # La ventana de reserva (60 días) siempre toca el mes siguiente.
        self.assertContains(response, f"mes={next_month.strftime('%Y-%m')}")



class OnlinePaymentsDisabledTests(TurnosFixtureMixin, TestCase):
    """Primera versión: la integración existe pero está apagada en el servidor."""

    def setUp(self):
        self.complex = self.make_complex(booking_price=Decimal("8000"))
        self.complex.mp_alias = "padel.principal"
        self.complex.mp_access_token = "APP_USR-12345678901234567890"
        self.complex.mp_webhook_secret = "secreto-webhook-1234567890"
        self.complex.save(
            update_fields=[
                "booking_price",
                "mp_alias",
                "mp_access_token_encrypted",
                "mp_webhook_secret_encrypted",
            ]
        )
        self.court = self.make_court(self.complex)
        self.day = timezone.localdate() + timedelta(days=5)
        self.make_schedule(self.complex, day_type=schedule_service.day_type_for(self.day))
        self.admin = User.objects.create_superuser(
            "admin-sin-online", password="clave-segura-123"
        )

    def reserve(self, **overrides):
        data = {
            "complejo": self.complex.slug,
            "fecha": self.day.isoformat(),
            "cancha": self.court.pk,
            "hora": "09:00",
            "nombre": "Ana Manual",
            "telefono": "2215551234",
            "nota": "",
        }
        data.update(overrides)
        return self.client.post(reverse("turno_reservar"), data)

    def make_pending(self):
        return self.make_booking(
            self.complex,
            self.court,
            self.day,
            time(10, 30),
            status="pending_payment",
            customer_name="Pago deshabilitado",
            customer_phone="2215551234",
            price=Decimal("8000"),
            deposit_amount=Decimal("2400"),
            payment_expires_at=timezone.now() + timedelta(minutes=15),
            payment_provider="mercadopago",
            payment_status="pending",
        )

    def test_public_reservation_stays_manual_even_with_mp_configured(self):
        self.assertFalse(self.complex.online_payments_enabled)
        with mock.patch("core.views.notify_court_booking") as notify:
            response = self.reserve()
        self.assertEqual(response.status_code, 302)
        booking = CourtBooking.objects.get()
        self.assertEqual(booking.status, "booked")
        self.assertIsNone(booking.payment_expires_at)
        self.assertEqual(booking.payment_status, "")
        self.assertFalse(booking.payment_provider)
        notify.assert_called_once()

        confirmation = self.client.get(
            reverse("turno_confirmado", args=[booking.code])
        )
        self.assertContains(confirmation, "El pago online no está disponible")
        self.assertNotContains(confirmation, "Pagar seña con Mercado Pago")

    def test_checkout_url_does_not_create_a_preference_or_cancel_a_hold(self):
        booking = self.make_pending()
        booking.payment_expires_at = timezone.now() - timedelta(minutes=1)
        booking.save(update_fields=["payment_expires_at"])
        with mock.patch(
            "core.views.payments_service.create_checkout_preference"
        ) as create:
            response = self.client.get(reverse("turno_pagar", args=[booking.code]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "El pago online no está disponible")
        create.assert_not_called()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending_payment")
        self.assertEqual(booking.payment_preference_id, "")
        self.assertEqual(booking.payment_init_point, "")
        self.assertFalse(CourtPayment.objects.filter(booking=booking).exists())

    def test_return_does_not_fetch_apply_or_cancel_a_payment(self):
        booking = self.make_pending()
        # Aun con la retención vencida, el retorno deshabilitado no debe
        # ejecutar el mantenimiento que cancelaría la reserva.
        booking.payment_expires_at = timezone.now() - timedelta(minutes=1)
        booking.save(update_fields=["payment_expires_at"])
        with mock.patch(
            "core.views.payments_service.fetch_payment"
        ) as fetch, mock.patch("core.views._apply_mp_payment") as apply:
            response = self.client.get(
                reverse("turno_pago_retorno", args=[booking.code]),
                {"payment_id": "payment-123"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "El pago online no está disponible")
        fetch.assert_not_called()
        apply.assert_not_called()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending_payment")
        self.assertFalse(booking.is_paid)
        self.assertFalse(CourtPayment.objects.filter(booking=booking).exists())

    def test_webhook_is_acked_without_processing_or_moving_cash(self):
        booking = self.make_pending()
        with mock.patch(
            "core.views.payments_service.fetch_payment"
        ) as fetch:
            response = self.client.post(
                reverse("turno_pago_webhook", args=[self.complex.slug]),
                data=json.dumps(
                    {"type": "payment", "data": {"id": "payment-123"}}
                ),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get("ignored"), "online_payments_disabled")
        fetch.assert_not_called()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending_payment")
        self.assertFalse(booking.is_paid)
        self.assertFalse(CourtPayment.objects.filter(booking=booking).exists())

    def test_settings_page_reports_disabled_without_requiring_secrets(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("panel_turnos_ajustes"),
            {"complejo": self.complex.slug},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "El pago online está deshabilitado en esta versión")
        self.assertContains(response, "Cobro online de la seña (deshabilitado)")
        self.assertNotContains(response, "falta el secreto de firma")

    def test_manual_collection_and_refund_keep_working(self):
        booking = self.make_pending()
        cash_service.record_payment(
            booking,
            kind="deposit",
            method="cash",
            amount=Decimal("2400.00"),
            user=self.admin,
            note="Cobro manual habilitado",
        )
        booking.refresh_from_db()
        self.assertTrue(booking.is_paid)

        first = cash_service.record_manual_refund(
            booking,
            Decimal("1000.00"),
            "cash",
            self.admin,
            note="Devolución manual",
            idempotency_key="sin-online-1",
        )
        second = cash_service.record_manual_refund(
            booking,
            Decimal("1000.00"),
            "cash",
            self.admin,
            note="Devolución manual",
            idempotency_key="sin-online-1",
        )
        self.assertEqual(first.pk, second.pk)
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal("1400.00"))
        self.assertFalse(booking.balance_paid)
        self.assertEqual(
            CourtPayment.objects.filter(booking=booking, kind="refund").count(), 1
        )
